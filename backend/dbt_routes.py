from backend.dbt_cloud_routes import register_dbt_cloud_routes
from backend.dbt_package_routes import register_dbt_package_routes


def register_dbt_agent_routes(app, get_db, build_session_bundle, upload_folder, call_ai=None):
    register_dbt_package_routes(app, get_db, upload_folder)
    register_dbt_cloud_routes(app, build_session_bundle, call_ai=call_ai)
