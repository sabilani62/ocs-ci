import datetime
import logging
import os
import pickle
import re
import time
import traceback

import yaml
from gevent import sleep
from libcloud.common.exceptions import BaseHTTPError
from libcloud.common.types import LibcloudError
from libcloud.compute.providers import get_driver
from libcloud.compute.types import Provider

from ocs_ci.ocs.ocp import OCP
from ocs_ci.utility.retry import retry
from ocs_ci.ocs.ceph import RolesContainer, CommandFailed, Ceph, CephNode
from ocs_ci.ocs.clients import WinNode
from ocs_ci.ocs.openstack import CephVMNode
from ocs_ci.ocs.parallel import parallel

log = logging.getLogger(__name__)


def create_ceph_nodes(cluster_conf, inventory, osp_cred, run_id, instances_name=None):
    osp_glbs = osp_cred.get('globals')
    os_cred = osp_glbs.get('openstack-credentials')
    params = dict()
    ceph_cluster = cluster_conf.get('ceph-cluster')
    if ceph_cluster.get('inventory'):
        inventory_path = os.path.abspath(ceph_cluster.get('inventory'))
        with open(inventory_path, 'r') as inventory_stream:
            inventory = yaml.safe_load(inventory_stream)
    params['cloud-data'] = inventory.get('instance').get('setup')
    params['username'] = os_cred['username']
    params['password'] = os_cred['password']
    params['auth-url'] = os_cred['auth-url']
    params['auth-version'] = os_cred['auth-version']
    params['tenant-name'] = os_cred['tenant-name']
    params['service-region'] = os_cred['service-region']
    params['keypair'] = os_cred.get('keypair', None)
    ceph_nodes = dict()
    if inventory.get('instance').get('create'):
        if ceph_cluster.get('image-name'):
            params['image-name'] = ceph_cluster.get('image-name')
        else:
            params['image-name'] = inventory.get('instance').get('create').get('image-name')
        params['cluster-name'] = ceph_cluster.get('name')
        params['vm-size'] = inventory.get('instance').get('create').get('vm-size')
        if params.get('root-login') is False:
            params['root-login'] = False
        else:
            params['root-login'] = True
        with parallel() as p:
            for node in range(1, 100):
                node = "node" + str(node)
                if not ceph_cluster.get(node):
                    break
                node_dict = ceph_cluster.get(node)
                node_params = params.copy()
                node_params['role'] = RolesContainer(node_dict.get('role'))
                role = node_params['role']
                user = os.getlogin()
                if instances_name:
                    node_params['node-name'] = "{}-{}-{}-{}-{}".format(
                        node_params.get('cluster-name', 'ceph'), instances_name, run_id, node, '+'.join(role))
                else:
                    node_params['node-name'] = "{}-{}-{}-{}-{}".format(
                        node_params.get('cluster-name', 'ceph'), user, run_id, node, '+'.join(role))
                if node_dict.get('no-of-volumes'):
                    node_params['no-of-volumes'] = node_dict.get('no-of-volumes')
                    node_params['size-of-disks'] = node_dict.get('disk-size')
                if node_dict.get('image-name'):
                    node_params['image-name'] = node_dict.get('image-name')
                if node_dict.get('cloud-data'):
                    node_params['cloud-data'] = node_dict.get('cloud-data')
                p.spawn(setup_vm_node, node, ceph_nodes, **node_params)
    log.info("Done creating nodes")
    return ceph_nodes


def setup_vm_node(node, ceph_nodes, **params):
    ceph_nodes[node] = CephVMNode(**params)


def get_openstack_driver(yaml):
    OpenStack = get_driver(Provider.OPENSTACK)
    glbs = yaml.get('globals')
    os_cred = glbs.get('openstack-credentials')
    username = os_cred['username']
    password = os_cred['password']
    auth_url = os_cred['auth-url']
    auth_version = os_cred['auth-version']
    tenant_name = os_cred['tenant-name']
    service_region = os_cred['service-region']
    driver = OpenStack(
        username,
        password,
        ex_force_auth_url=auth_url,
        ex_force_auth_version=auth_version,
        ex_tenant_name=tenant_name,
        ex_force_service_region=service_region,
        ex_domain_name='redhat.com'
    )
    return driver


def cleanup_ceph_nodes(osp_cred, pattern=None, timeout=300):
    user = os.getlogin()
    name = pattern if pattern else '-{user}-'.format(user=user)
    driver = get_openstack_driver(osp_cred)
    timeout = datetime.timedelta(seconds=timeout)
    with parallel() as p:
        for node in driver.list_nodes():
            if name in node.name:
                for ip in node.public_ips:
                    log.info("removing ip %s from node %s", ip, node.name)
                    driver.ex_detach_floating_ip_from_node(node, ip)
                starttime = datetime.datetime.now()
                log.info(
                    "Destroying node {node_name} with {timeout} timeout".format(node_name=node.name, timeout=timeout))
                while True:
                    try:
                        p.spawn(node.destroy)
                        break
                    except AttributeError:
                        if datetime.datetime.now() - starttime > timeout:
                            raise RuntimeError(
                                "Failed to destroy node {node_name} with {timeout} timeout:\n{stack_trace}".format(
                                    node_name=node.name,
                                    timeout=timeout, stack_trace=traceback.format_exc()))
                        else:
                            sleep(1)
                sleep(5)
    with parallel() as p:
        for fips in driver.ex_list_floating_ips():
            if fips.node_id is None:
                log.info("Releasing ip %s", fips.ip_address)
                driver.ex_delete_floating_ip(fips)
    with parallel() as p:
        errors = {}
        for volume in driver.list_volumes():
            if volume.name is None:
                log.info("Volume has no name, skipping")
            elif name in volume.name:
                log.info("Removing volume %s", volume.name)
                sleep(10)
                try:
                    volume.destroy()
                except BaseHTTPError as e:
                    log.error(e, exc_info=True)
                    errors.update({volume.name: e.message})
        if errors:
            for vol, err in errors.items():
                log.error("Error destroying {vol}: {err}".format(vol=vol, err=err))
            raise RuntimeError("Encountered errors during volume deletion. Volume names and messages have been logged.")


def keep_alive(ceph_nodes):
    for node in ceph_nodes:
        node.exec_command(cmd='uptime', check_ec=False)


def setup_repos(ceph, base_url, installer_url=None):
    repos = ['MON', 'OSD', 'Tools', 'Calamari', 'Installer']
    base_repo = generate_repo_file(base_url, repos)
    base_file = ceph.write_file(
        sudo=True,
        file_name='/etc/yum.repos.d/rh_ceph.repo',
        file_mode='w')
    base_file.write(base_repo)
    base_file.flush()
    if installer_url is not None:
        installer_repos = ['Agent', 'Main', 'Installer']
        inst_repo = generate_repo_file(installer_url, installer_repos)
        log.info("Setting up repo on %s", ceph.hostname)
        inst_file = ceph.write_file(
            sudo=True,
            file_name='/etc/yum.repos.d/rh_ceph_inst.repo',
            file_mode='w')
        inst_file.write(inst_repo)
        inst_file.flush()


def check_ceph_healthly(ceph_mon, num_osds, num_mons, mon_container=None, timeout=300):
    """
    Function to check ceph is in healthy state

    Args:
       ceph_mon: monitor node
       num_osds: number of osds in cluster
       num_mons: number of mons in cluster
       mon_container: monitor container name if monitor is placed in the container
       timeout: 300 seconds(default) max time to check
         if cluster is not healthy within timeout period
                return 1

    Returns:
       return 0 when ceph is in healthy state, else 1
    """

    timeout = datetime.timedelta(seconds=timeout)
    starttime = datetime.datetime.now()
    lines = None
    pending_states = ['peering', 'activating', 'creating']
    valid_states = ['active+clean']

    while datetime.datetime.now() - starttime <= timeout:
        if mon_container:
            out, err = ceph_mon.exec_command(cmd='sudo docker exec {container} ceph -s'.format(container=mon_container))
        else:
            out, err = ceph_mon.exec_command(cmd='sudo ceph -s')
        lines = out.read().decode()

        if not any(state in lines for state in pending_states):
            if all(state in lines for state in valid_states):
                break
        sleep(5)
    log.info(lines)
    if not all(state in lines for state in valid_states):
        log.error("Valid States are not found in the health check")
        return 1
    match = re.search(r"(\d+)\s+osds:\s+(\d+)\s+up,\s+(\d+)\s+in", lines)
    all_osds = int(match.group(1))
    up_osds = int(match.group(2))
    in_osds = int(match.group(3))
    if num_osds != all_osds:
        log.error("Not all osd's are up. %s / %s" % (num_osds, all_osds))
        return 1
    if up_osds != in_osds:
        log.error("Not all osd's are in. %s / %s" % (up_osds, all_osds))
        return 1

    # attempt luminous pattern first, if it returns none attempt jewel pattern
    match = re.search(r"(\d+) daemons, quorum", lines)
    if not match:
        match = re.search(r"(\d+) mons at", lines)
    all_mons = int(match.group(1))
    if all_mons != num_mons:
        log.error("Not all monitors are in cluster")
        return 1
    if "HEALTH_ERR" in lines:
        log.error("HEALTH in ERROR STATE")
        return 1
    return 0


def generate_repo_file(base_url, repos):
    return Ceph.generate_repository_file(base_url, repos)


def get_iso_file_url(base_url):
    return Ceph.get_iso_file_url(base_url)


def create_ceph_conf(fsid, mon_hosts, pg_num='128', pgp_num='128', size='2',
                     auth='cephx', pnetwork='172.16.0.0/12',
                     jsize='1024'):
    fsid = 'fsid = ' + fsid + '\n'
    mon_init_memb = 'mon initial members = '
    mon_host = 'mon host = '
    public_network = 'public network = ' + pnetwork + '\n'
    auth = 'auth cluster required = cephx\nauth service \
            required = cephx\nauth client required = cephx\n'
    jsize = 'osd journal size = ' + jsize + '\n'
    size = 'osd pool default size = ' + size + '\n'
    pgnum = 'osd pool default pg num = ' + pg_num + '\n'
    pgpnum = 'osd pool default pgp num = ' + pgp_num + '\n'
    for mhost in mon_hosts:
        mon_init_memb = mon_init_memb + mhost.shortname + ','
        mon_host = mon_host + mhost.internal_ip + ','
    mon_init_memb = mon_init_memb[:-1] + '\n'
    mon_host = mon_host[:-1] + '\n'
    conf = '[global]\n'
    conf = conf + fsid + mon_init_memb + mon_host + public_network + auth + size + jsize + pgnum + pgpnum
    return conf


def setup_deb_repos(node, ubuntu_repo):
    node.exec_command(cmd='sudo rm -f /etc/apt/sources.list.d/*')
    repos = ['MON', 'OSD', 'Tools']
    for repo in repos:
        cmd = 'sudo echo deb ' + ubuntu_repo + '/{0}'.format(repo) + \
              ' $(lsb_release -sc) main'
        node.exec_command(cmd=cmd + ' > ' + "/tmp/{0}.list".format(repo))
        node.exec_command(cmd='sudo cp /tmp/{0}.list /etc/apt/sources.list.d/'.format(repo))
    ds_keys = ['https://www.redhat.com/security/897da07a.txt',
               'https://www.redhat.com/security/f21541eb.txt',
               # 'https://prodsec.redhat.com/keys/00da75f2.txt',
               # TODO: replace file file.rdu.redhat.com/~kdreyer with prodsec.redhat.com when it's back
               'http://file.rdu.redhat.com/~kdreyer/keys/00da75f2.txt',
               'https://www.redhat.com/security/data/fd431d51.txt']

    for key in ds_keys:
        wget_cmd = 'sudo wget -O - ' + key + ' | sudo apt-key add -'
        node.exec_command(cmd=wget_cmd)
    node.exec_command(cmd='sudo apt-get update')


def setup_deb_cdn_repo(node, build=None):
    user = 'redhat'
    passwd = 'OgYZNpkj6jZAIF20XFZW0gnnwYBjYcmt7PeY76bLHec9'
    num = build.split('.')[0]
    cmd = 'umask 0077; echo deb https://{user}:{passwd}@rhcs.download.redhat.com/{num}-updates/Tools ' \
          '$(lsb_release -sc) main | tee /etc/apt/sources.list.d/Tools.list'.format(user=user, passwd=passwd, num=num)
    node.exec_command(sudo=True, cmd=cmd)
    node.exec_command(sudo=True, cmd='wget -O - https://www.redhat.com/security/fd431d51.txt | apt-key add -')
    node.exec_command(sudo=True, cmd='apt-get update')


def setup_cdn_repos(ceph_nodes, build=None):
    repos_13x = ['rhel-7-server-rhceph-1.3-mon-rpms',
                 'rhel-7-server-rhceph-1.3-osd-rpms',
                 'rhel-7-server-rhceph-1.3-calamari-rpms',
                 'rhel-7-server-rhceph-1.3-installer-rpms',
                 'rhel-7-server-rhceph-1.3-tools-rpms']

    repos_20 = ['rhel-7-server-rhceph-2-mon-rpms',
                'rhel-7-server-rhceph-2-osd-rpms',
                'rhel-7-server-rhceph-2-tools-rpms',
                'rhel-7-server-rhscon-2-agent-rpms',
                'rhel-7-server-rhscon-2-installer-rpms',
                'rhel-7-server-rhscon-2-main-rpms']

    repos_30 = ['rhel-7-server-rhceph-3-mon-rpms',
                'rhel-7-server-rhceph-3-osd-rpms',
                'rhel-7-server-rhceph-3-tools-rpms',
                'rhel-7-server-extras-rpms']

    repos = None
    if build.startswith('1'):
        repos = repos_13x
    elif build.startswith('2'):
        repos = repos_20
    elif build.startswith('3'):
        repos = repos_30
    with parallel() as p:
        for node in ceph_nodes:
            p.spawn(set_cdn_repo, node, repos)


def set_cdn_repo(node, repos):
    for repo in repos:
        node.exec_command(
            sudo=True, cmd='subscription-manager repos --enable={r}'.format(r=repo))
    # node.exec_command(sudo=True, cmd='subscription-manager refresh')


def update_ca_cert(node, cert_url, timeout=120):
    if node.pkg_type == 'deb':
        cmd = 'cd /usr/local/share/ca-certificates/ && {{ sudo curl -O {url} ; cd -; }}'.format(url=cert_url)
        node.exec_command(cmd=cmd, timeout=timeout)
        node.exec_command(cmd='sudo update-ca-certificates', timeout=timeout)
    else:
        cmd = 'cd /etc/pki/ca-trust/source/anchors && {{ sudo curl -O {url} ; cd -; }}'.format(url=cert_url)
        node.exec_command(cmd=cmd, timeout=timeout)
        node.exec_command(cmd='sudo update-ca-trust extract', timeout=timeout)


def write_docker_daemon_json(json_text, node):
    """
    Write given string to /etc/docker/daemon/daemon
    Args:
        json_text: json string
        node (ceph.ceph.CephNode): Ceph node object
    """
    node.write_docker_daemon_json(json_text)


def search_ethernet_interface(ceph_node, ceph_node_list):
    """
    Search interface on the given node node which allows every node in the cluster accesible by it's shortname.

    Args:
        ceph_node (ceph.ceph.CephNode): node where check is performed
        ceph_node_list(list): node list to check
    """
    return ceph_node.search_ethernet_interface(ceph_node_list)


def open_firewall_port(ceph_node, port, protocol):
    """
    Opens firewall ports for given node
    Args:
        ceph_node (ceph.ceph.CephNode): ceph node
        port (str): port
        protocol (str): protocol
    """
    ceph_node.open_firewall_port(port, protocol)


def config_ntp(ceph_node):
    ceph_node.exec_command(
        cmd="sudo sed -i '/server*/d' /etc/ntp.conf",
        long_running=True)
    ceph_node.exec_command(
        cmd="echo 'server clock.corp.redhat.com iburst' | sudo tee -a /etc/ntp.conf",
        long_running=True)
    ceph_node.exec_command(cmd="sudo ntpd -gq", long_running=True)
    ceph_node.exec_command(cmd="sudo systemctl enable ntpd", long_running=True)
    ceph_node.exec_command(cmd="sudo systemctl start ntpd", long_running=True)


def get_ceph_versions(ceph_nodes, containerized=False):
    """
    Log and return the ceph or ceph-ansible versions for each node in the cluster.

    Args:
        ceph_nodes: nodes in the cluster
        containerized: is the cluster containerized or not

    Returns:
        A dict of the name / version pair for each node or container in the cluster
    """
    versions_dict = {}

    for node in ceph_nodes:
        try:
            if node.role == 'installer':
                if node.pkg_type == 'rpm':
                    out, rc = node.exec_command(cmd='rpm -qa | grep ceph-ansible')
                else:
                    out, rc = node.exec_command(cmd='dpkg -s ceph-ansible')
                output = out.read().decode().rstrip()
                log.info(output)
                versions_dict.update({node.shortname: output})

            else:
                if containerized:
                    containers = []
                    if node.role == 'client':
                        pass
                    else:
                        out, rc = node.exec_command(sudo=True, cmd='docker ps --format "{{.Names}}"')
                        output = out.read().decode()
                        containers = [container for container in output.split('\n') if container != '']
                        log.info("Containers: {}".format(containers))

                    for container_name in containers:
                        out, rc = node.exec_command(
                            sudo=True, cmd='sudo docker exec {container} ceph --version'.format(
                                container=container_name))
                        output = out.read().decode().rstrip()
                        log.info(output)
                        versions_dict.update({container_name: output})

                else:
                    out, rc = node.exec_command(cmd='ceph --version')
                    output = out.read().decode().rstrip()
                    log.info(output)
                    versions_dict.update({node.shortname: output})

        except CommandFailed:
            log.info("No ceph versions on {}".format(node.shortname))

    return versions_dict


def hard_reboot(gyaml, name=None):
    user = os.getlogin()
    if name is None:
        name = 'ceph-' + user
    driver = get_openstack_driver(gyaml)
    for node in driver.list_nodes():
        if node.name.startswith(name):
            log.info('Hard-rebooting %s' % node.name)
            driver.ex_hard_reboot_node(node)

    return 0


def node_power_failure(gyaml, sleep_time=300, name=None):
    user = os.getlogin()
    if name is None:
        name = 'ceph-' + user
    driver = get_openstack_driver(gyaml)
    for node in driver.list_nodes():
        if node.name.startswith(name):
            log.info('Doing power-off on %s' % node.name)
            driver.ex_stop_node(node)
            time.sleep(20)
            op = driver.ex_get_node_details(node)
            if op.state == 'stopped':
                log.info('Node stopped successfully')
            time.sleep(sleep_time)
            log.info('Doing power-on on %s' % node.name)
            driver.ex_start_node(node)
            time.sleep(20)
            op = driver.ex_get_node_details(node)
            if op.state == 'running':
                log.info('Node restarted successfully')
            time.sleep(20)
    return 0


def get_root_permissions(node, path):
    """
    Transfer ownership of root to current user for the path given. Recursive.
    Args:
        node(ceph.ceph.CephNode):
        path: file path
    """
    node.obtain_root_permissions(path)


def get_public_network():
    """
    Get the configured public network subnet for nodes in the cluster.

    Returns:
        (str) public network subnet
    """
    return "10.0.144.0/22"  # TODO: pull from configuration file


@retry(LibcloudError, tries=5, delay=15)
def create_nodes(conf, inventory, osp_cred, run_id, instances_name=None):
    log.info("Destroying existing osp instances")
    cleanup_ceph_nodes(osp_cred, instances_name)
    ceph_cluster_dict = {}
    log.info('Creating osp instances')
    for cluster in conf.get('globals'):
        ceph_vmnodes = create_ceph_nodes(cluster, inventory, osp_cred, run_id, instances_name)
        ceph_nodes = []
        clients = []
        for node in ceph_vmnodes.values():
            if node.role == 'win-iscsi-clients':
                clients.append(WinNode(ip_address=node.ip_address,
                                       private_ip=node.get_private_ip()))
            else:
                ceph = CephNode(username='cephuser',
                                password='cephuser',
                                root_password='passwd',
                                root_login=node.root_login,
                                role=node.role,
                                no_of_volumes=node.no_of_volumes,
                                ip_address=node.ip_address,
                                private_ip=node.get_private_ip(),
                                hostname=node.hostname,
                                ceph_vmnode=node)
                ceph_nodes.append(ceph)
        cluster_name = cluster.get('ceph-cluster').get('name', 'ceph')
        ceph_cluster_dict[cluster_name] = Ceph(cluster_name, ceph_nodes)
    # TODO: refactor cluster dict to cluster list
    log.info('Done creating osp instances')
    log.info("Waiting for Floating IPs to be available")
    log.info("Sleeping 15 Seconds")
    time.sleep(15)
    for cluster_name, cluster in ceph_cluster_dict.items():
        for instance in cluster:
            instance.connect()
    return ceph_cluster_dict, clients


def store_cluster_state(ceph_cluster_object, ceph_clusters_file_name):
    cn = open(ceph_clusters_file_name, 'w+b')
    pickle.dump(ceph_cluster_object, cn)
    cn.close()
    log.info("ceph_clusters_file %s", ceph_clusters_file_name)


def create_oc_resource(
    template_name,
    cluster_path,
    _templating,
    template_data=None,
    template_dir="ocs-deployment",
):
    """
    Create an oc resource after rendering the specified template with
    the rook data from cluster_conf.

    Args:
        template_name (str): Name of the ocs-deployment config template
        cluster_path (str): Path to cluster directory, where files will be
            written
        _templating (Templating): Object of Templating class used for
            templating
        template_data (dict): Data for render template (default: {})
        template_dir (str): Directory under templates dir where template
            exists (default: ocs-deployment)
    """
    if template_data is None:
        template_data = {}
    template_path = os.path.join(template_dir, template_name)
    template = _templating.render_template(template_path, template_data)
    cfg_file = os.path.join(cluster_path, template_name)
    with open(cfg_file, "w") as f:
        f.write(template)
    log.info(f"Creating rook resource from {template_name}")
    occli = OCP()
    occli.create(cfg_file)


def apply_oc_resource(
    template_name,
    cluster_path,
    _templating,
    template_data=None,
    template_dir="ocs-deployment",
):
    """
    Apply an oc resource after rendering the specified template with
    the rook data from cluster_conf.

    Args:
        template_name (str): Name of the ocs-deployment config template
        cluster_path (str): Path to cluster directory, where files will be
            written
        _templating (Templating): Object of Templating class used for
            templating
        template_data (dict): Data for render template (default: {})
        template_dir (str): Directory under templates dir where template
            exists (default: ocs-deployment)
    """
    if template_data is None:
        template_data = {}
    template_path = os.path.join(template_dir, template_name)
    template = _templating.render_template(template_path, template_data)
    cfg_file = os.path.join(cluster_path, template_name)
    with open(cfg_file, "w") as f:
        f.write(template)
    log.info(f"Applying rook resource from {template_name}")
    occli = OCP()
    occli.apply(cfg_file)
