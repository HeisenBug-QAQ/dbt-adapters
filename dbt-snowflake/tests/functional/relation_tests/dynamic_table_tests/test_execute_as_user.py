"""
Functional tests for the dynamic table `execute_as_user` config.

A dynamic table's refresh runs as an internal identity by default, so a CURRENT_USER()-gated
masking or row-access policy evaluates against that identity and can bake masked values into the
table's storage. `execute_as_user` emits `EXECUTE AS USER <user>` in the CREATE/REPLACE DDL, so the
*first* refresh already runs as the intended user -- which a post_hook cannot achieve, because dbt
refreshes the dynamic table before post_hooks run.

Snowflake requires two grants for the refresh to succeed, both set up by the `service_user`
fixture below: `IMPERSONATE` on the user, and the dynamic table's owner role granted to that user.
Without the second, the DDL succeeds but the refresh fails -- so the fixture is part of the
contract these tests document.

These require a live Snowflake connection.
"""

import os

import pytest

from dbt.tests.util import run_dbt, run_dbt_and_capture

from tests.functional.utils import describe_dynamic_table, update_model


SEED = """
id,value
1,alice
2,bob
""".strip()

# The refresh identity must be an existing Snowflake user, and Snowflake requires two grants for
# the refresh to succeed: IMPERSONATE on that user to the test role, and the test role granted to
# the user. Both are account-level operations, so the user is provisioned out of band (like
# DBT_TEST_USER_1/2/3) rather than created by the tests.
EXECUTE_AS_USER = os.getenv("SNOWFLAKE_TEST_EXECUTE_AS_USER", "")

# A warehouse change can only be observed with two real, distinct warehouses.
ALT_WAREHOUSE = os.getenv("SNOWFLAKE_TEST_ALT_WAREHOUSE", "DBT_TESTING")

pytestmark = pytest.mark.skipif(
    not EXECUTE_AS_USER,
    reason="SNOWFLAKE_TEST_EXECUTE_AS_USER is not set. It must name an existing user with "
    "`GRANT IMPERSONATE ON USER <user> TO ROLE <test role>` and "
    "`GRANT ROLE <test role> TO USER <user>` already applied.",
)


def _model(execute_as_user=None, warehouse="DBT_TESTING", cluster_by=None):
    identity = f",\n    execute_as_user='{execute_as_user}'" if execute_as_user else ""
    clustering = f",\n    cluster_by={cluster_by!r}" if cluster_by else ""
    return (
        "{{ config(\n"
        "    materialized='dynamic_table',\n"
        f"    snowflake_warehouse='{warehouse}',\n"
        "    refresh_mode='FULL',\n"
        "    scheduler='disable'"
        f"{identity}{clustering}\n"
        ") }}\n"
        "select id, value from {{ ref('my_seed') }}"
    )


def _created_on(project):
    """First column of SHOW DYNAMIC TABLES; changes only if the object is dropped/recreated, so a
    stable value proves an in-place ALTER rather than a rebuild."""
    rows = project.run_sql(
        f"show dynamic tables like '%DT_IDENTITY%' in schema "
        f"{project.database}.{project.test_schema}",
        fetch="all",
    )
    for row in rows:
        if str(row[1]).upper() == "DT_IDENTITY":
            return row[0]
    return None


class ExecuteAsUserBase:
    """Shared fixtures. The refresh identity is pre-provisioned (see EXECUTE_AS_USER)."""

    SVC_USER = EXECUTE_AS_USER

    @pytest.fixture(scope="class", autouse=True)
    def seeds(self):
        yield {"my_seed.csv": SEED}

    @pytest.fixture(scope="function", autouse=True)
    def setup(self, project):
        run_dbt(["seed"])
        yield
        project.run_sql(f"drop schema if exists {project.test_schema} cascade")


class TestExecuteAsUserOnCreate(ExecuteAsUserBase):
    """The identity is emitted in the CREATE DDL, so it is in place for the first refresh."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model(EXECUTE_AS_USER)}

    def test_execute_as_user_is_set_on_create(self, project):
        results = run_dbt(["run"])  # must succeed: CREATE *and* the initial refresh
        assert len(results) == 1
        dt = describe_dynamic_table(project, "dt_identity")
        assert str(dt.execute_as_user).upper() == self.SVC_USER

    def test_execute_as_user_survives_full_refresh(self, project):
        run_dbt(["run"])
        # confirm it was set BEFORE the rebuild, else this cannot distinguish "survived a replace"
        # from "set for the first time"
        assert str(describe_dynamic_table(project, "dt_identity").execute_as_user).upper() == (
            self.SVC_USER.upper()
        )
        before = _created_on(project)
        run_dbt(["run", "--full-refresh"])
        assert _created_on(project) != before, "expected --full-refresh to rebuild the object"
        dt = describe_dynamic_table(project, "dt_identity")
        # re-emitted from config on replace -- the post_hook workaround loses it here
        assert str(dt.execute_as_user).upper() == self.SVC_USER


class TestExecuteAsUserUnset(ExecuteAsUserBase):
    """Control: without the config, no clause is emitted and nothing breaks."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model()}

    def test_absent_when_not_configured(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1
        dt = describe_dynamic_table(project, "dt_identity")
        assert dt.execute_as_user is None


class TestExecuteAsUserChangeAppliesInPlace(ExecuteAsUserBase):
    """Adding the identity to an existing dynamic table is a detected config change that ALTER
    applies in place (created_on unchanged), and removing it emits UNSET."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model()}

    def test_set_then_unset_via_alter(self, project):
        run_dbt(["run"])
        assert describe_dynamic_table(project, "dt_identity").execute_as_user is None
        before = _created_on(project)

        # add the identity -> ALTER ... SET execute as user
        update_model(project, "dt_identity", _model(self.SVC_USER))
        assert len(run_dbt(["run"])) == 1
        dt = describe_dynamic_table(project, "dt_identity")
        assert str(dt.execute_as_user).upper() == self.SVC_USER
        assert _created_on(project) == before, "expected in-place ALTER, not a rebuild"

        # remove it -> ALTER ... UNSET execute as user
        update_model(project, "dt_identity", _model())
        assert len(run_dbt(["run"])) == 1
        assert describe_dynamic_table(project, "dt_identity").execute_as_user is None
        assert _created_on(project) == before, "expected in-place ALTER, not a rebuild"


class TestExecuteAsUserCaseInsensitive(ExecuteAsUserBase):
    """Snowflake folds the identifier to upper case, so a lower-case config value must not be
    seen as a change on every run (which would emit a spurious ALTER each time)."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model(EXECUTE_AS_USER)}

    def test_lowercase_config_is_not_a_change(self, project):
        run_dbt(["run"])
        before = _created_on(project)
        # same user, written lower case -> must not be seen as a change
        update_model(project, "dt_identity", _model(EXECUTE_AS_USER.lower()))
        _, logs = run_dbt_and_capture(["--debug", "run"])
        # the ALTER log marker is the discriminator: created_on is stable either way, so asserting
        # only on it would pass even with normalization disabled
        assert "Applying UPDATE EXECUTE AS USER" not in logs
        dt = describe_dynamic_table(project, "dt_identity")
        assert str(dt.execute_as_user).upper() == self.SVC_USER.upper()
        assert _created_on(project) == before


class MaskingBase(ExecuteAsUserBase):
    """Applies a CURRENT_USER()-gated masking policy to the seed column the dynamic table selects.

    The policy returns the real value only for the service user, so what the dynamic table stores
    depends entirely on which identity ran its refresh. This is the acceptance scenario:
    dbt refreshes a dynamic table BEFORE post_hooks run, so a post_hook that sets the identity
    cannot prevent the first refresh from baking masked values into storage. Emitting
    `EXECUTE AS USER` in the CREATE DDL can.
    """

    @pytest.fixture(scope="function", autouse=True)
    def masking_policy(self, project, setup):
        fqn_seed = f"{project.database}.{project.test_schema}.my_seed"
        policy = f"{project.database}.{project.test_schema}.mask_value"
        project.run_sql(
            f"create or replace masking policy {policy} as (val string) returns string -> "
            f"case when current_user() = '{self.SVC_USER}' then val else '***masked***' end"
        )
        project.run_sql(f"alter table {fqn_seed} modify column value set masking policy {policy}")
        yield

    @staticmethod
    def _stored_values(project):
        rows = project.run_sql(
            f"select value from {project.database}.{project.test_schema}.dt_identity order by id",
            fetch="all",
        )
        return [r[0] for r in rows]


class TestMaskedDataIsNotBakedInWithExecuteAsUser(MaskingBase):
    """With `execute_as_user`, the FIRST refresh runs as the service user, so the dynamic table
    stores the real values rather than the masked placeholder."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model(EXECUTE_AS_USER)}

    def test_first_refresh_stores_unmasked_data(self, project):
        assert len(run_dbt(["run"])) == 1
        assert self._stored_values(project) == ["alice", "bob"]


class TestMaskedDataIsBakedInWithoutExecuteAsUser(MaskingBase):
    """Control (documents the gap this config closes): with no `execute_as_user`, the refresh runs
    as the default identity, the policy masks, and the masked placeholder is written to storage --
    where it stays until the next refresh, no matter what a post_hook does afterwards."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model()}

    def test_first_refresh_stores_masked_data(self, project):
        assert len(run_dbt(["run"])) == 1
        assert self._stored_values(project) == ["***masked***", "***masked***"]


class TestExecuteAsUserAlongsideOtherAlters(ExecuteAsUserBase):
    """The identity ALTER is one statement in a chain: a run that also changes the warehouse and
    the clustering key must emit all of them as a correctly `;`-separated batch. Covers the
    semicolon placement that a lone identity change never exercises."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model(cluster_by=["id"])}

    def test_identity_change_batched_with_other_alters(self, project):
        run_dbt(["run"])
        dt = describe_dynamic_table(project, "dt_identity")
        assert dt.execute_as_user is None
        before = _created_on(project)

        # change warehouse + clustering key + add the identity, all in one run
        update_model(
            project,
            "dt_identity",
            _model(self.SVC_USER, warehouse=ALT_WAREHOUSE, cluster_by=["id", "value"]),
        )
        assert len(run_dbt(["run"])) == 1

        dt = describe_dynamic_table(project, "dt_identity")
        assert str(dt.execute_as_user).upper() == self.SVC_USER
        assert str(dt.snowflake_warehouse).upper() == ALT_WAREHOUSE.upper()
        assert "value" in str(dt.cluster_by).lower()
        assert _created_on(project) == before, "expected in-place ALTERs, not a rebuild"

    def test_identity_unset_batched_with_other_alters(self, project):
        update_model(project, "dt_identity", _model(self.SVC_USER, cluster_by=["id"]))
        run_dbt(["run", "--full-refresh"])
        before = _created_on(project)

        # remove the identity while also changing the warehouse -> UNSET must chain correctly
        update_model(project, "dt_identity", _model(warehouse=ALT_WAREHOUSE, cluster_by=["id"]))
        assert len(run_dbt(["run"])) == 1

        dt = describe_dynamic_table(project, "dt_identity")
        assert dt.execute_as_user is None
        assert str(dt.snowflake_warehouse).upper() == ALT_WAREHOUSE.upper()
        assert _created_on(project) == before


class TestExecuteAsUserChainedWithClusterByOnly(ExecuteAsUserBase):
    """Chaining with only a clustering-key change (warehouse and target_lag untouched), which is the
    combination that exercises the cluster_by arm of the statement-chaining logic."""

    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {"dt_identity.sql": _model(cluster_by=["id"])}

    def test_identity_and_cluster_by_change_together(self, project):
        run_dbt(["run"])
        before = _created_on(project)
        update_model(project, "dt_identity", _model(self.SVC_USER, cluster_by=["id", "value"]))
        assert len(run_dbt(["run"])) == 1
        dt = describe_dynamic_table(project, "dt_identity")
        assert str(dt.execute_as_user).upper() == self.SVC_USER.upper()
        assert "value" in str(dt.cluster_by).lower()
        assert _created_on(project) == before, "expected in-place ALTERs, not a rebuild"
