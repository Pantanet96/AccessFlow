from sqlalchemy import inspect


def test_manager_id_is_indexed(db_session):
    # list_users_for/assigned_active_count filter app_user by manager_id on
    # every users-list render; without an index that's a full table scan.
    insp = inspect(db_session.get_bind())
    indexed_cols = {
        col for ix in insp.get_indexes("app_user") for col in ix["column_names"]
    }
    assert "manager_id" in indexed_cols
