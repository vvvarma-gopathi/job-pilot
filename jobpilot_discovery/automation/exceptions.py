class AutomationSafetyError(RuntimeError):
    pass


class UnsupportedApplicationSite(AutomationSafetyError):
    pass


class UserChallengeRequired(AutomationSafetyError):
    pass
