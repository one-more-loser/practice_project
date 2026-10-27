"""Keep SQL candidate errors separate from a broken reward environment."""


class OracleEnvironmentError(RuntimeError):
    """Abort/void the rollout or evaluation; never turn infrastructure into zero."""


def is_candidate_sql_error(exc):
    if isinstance(exc, RuntimeError) and str(exc) == 'row_limit_exceeded':
        return True
    state = getattr(exc, 'sqlstate', None)
    # Data/syntax errors, explicit query cancellation (including our timeout),
    # and SQL program complexity belong to this candidate. Connection loss,
    # missing server/database, disk full and unknown internal errors do not.
    return isinstance(state, str) and (state[:2] in {'22', '42', '54'} or state == '57014')
