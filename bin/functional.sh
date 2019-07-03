#!/bin/bash
setup_aws_creds () {
  mkdir -p $(dirname $AWS_SHARED_CREDENTIALS_FILE)
  cat << EOF > $AWS_SHARED_CREDENTIALS_FILE
[$AWS_PROFILE]
aws_access_key_id = $AWS_ACCESS_KEY_ID
aws_secret_access_key = $AWS_SECRET_ACCESS_KEY
EOF
  mkdir -p $(dirname $AWS_CONFIG_FILE)
  cat << EOF > $AWS_CONFIG_FILE
[profile $AWS_PROFILE]
region = $AWS_REGION
output = text
EOF
}

setup_pull_secret () {
  mkdir -p $CI_PROJECT_DIR/data
  echo $PULL_SECRET | base64 -d > $CI_PROJECT_DIR/data/pull-secret
}

setup_ocsci_conf () {
  cat << EOF > $CI_PROJECT_DIR/ocs-ci.yaml
RUN:
  log_dir: $CI_PROJECT_DIR/logs
ENV_DATA:
  platform: "AWS"
  cluster_name: "$CLUSTER_USER-ocs-ci-$CI_PIPELINE_ID"
  region: $AWS_REGION
  base_domain: $AWS_DOMAIN
EOF
}

setup_aws_creds
setup_pull_secret
setup_ocsci_conf
