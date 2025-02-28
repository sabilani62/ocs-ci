# Writing tests

In this documentation we will inform you about basic structure where implement
code and some structure and objects you can use.

Please feel free to update this documentation because it will help others ppl
a lot!

## Pytest marks

We have predefined some of pytest marks you can use to decorate your tests.
You can find them defined in [pytest.ini](../pytest.ini) where we inform
pytest about those marks.

We have markers defined in pytest_customization package under
[marks.py](../ocs_ci/framework/pytest_customization/marks.py) plugin. From your tests you
can import directly from `ocsci.testlib` module with this statement:
`from ocsci.testlib import tier1` for example.


## Base test classes for teams

Those are located in [testlib.py](../ocs_ci/framework/testlib.py) which you can also
import from `ocsci.testlib` module with statement:
`from ocsci.testlib import manage` which is base test class for manage team.


## Constants and Defaults

Many of our tests utilize defaults and constants. These are both defined in
`ocs/constants.py` and `ocs/defaults.py` respectively. Constants and defaults
are fairly similar but functionally different which is why we have chosen
to separate them into their own modules.

If your test requires one of these you can easily import it.
If you intend to implement a new one (generally if more than one test will
utilize it), please consider whether or not that value might change between
different test executions. If it's something like a filepath (unchanging),
it's probably a constant. If tests may overwrite the value, it's most likely a
default.

Note these modules are not intended to be a dumping ground for any variable
your test might need. These are designed to be homes for widely used variables
that need to be consistent across a test execution. You can learn more from
viewing the existing constants and defaults in their respective modules.


## Other notes

Of course you can import in one line both team base class and marker with
statement: `from ocsci.testlib import manage, tier1`
