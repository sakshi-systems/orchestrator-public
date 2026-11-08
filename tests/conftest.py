def pytest_sessionfinish(session, exitstatus):
    # NO_TESTS_COLLECTED (5) is the success condition for the empty scaffold.
    # Once real tests exist they collect normally and this never fires.
    if exitstatus == 5:
        session.exitstatus = 0
