"""
Control flow classes for DSL task execution
"""


class EarlyReturn:
    """Special return value that signals the DSL runtime to stop executing tasks

    When a task returns an EarlyReturn instance, the runtime will:
    1. Stop executing remaining non-@always tasks
    2. Continue executing any remaining @always tasks
    3. Exit successfully with the provided message

    This allows for early successful exit when a condition is met
    (e.g., operator already deployed) while preserving cleanup tasks.
    """

    def __init__(self, message: str):
        self.message = message

    def __str__(self):
        return self.message

    def __repr__(self):
        return f"EarlyReturn({self.message!r})"
