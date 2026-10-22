"""The read routes and the closed gates of tests/test_api_topic_ideation.py,
the API connected as ai_smm_cc (conftest.py). Queuing a job is a business
command, which this role cannot make: it is re-run as ai_smm_cc_rw in
tests/mutation_role/ instead."""
from tests.test_api_topic_ideation import (  # noqa: F401
    api_settings,
    default_client,
    read_only_client,
    seed_job,
    test_a_viewer_reads_jobs_and_ideas,
    test_an_unknown_job_is_404,
    test_another_projects_rows_are_invisible,
    test_every_member_role_may_read,
    test_ideas_filter_by_job_and_status,
    test_jobs_are_paginated_newest_first_and_filterable,
    test_reads_work_while_the_gate_is_off,
    test_the_mutation_gate_refuses_first,
)
