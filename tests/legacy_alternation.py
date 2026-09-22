"""Keep deadline-policy regressions exercising persisted legacy phase states."""

from unittest import mock

from franta.scheduler import Scheduler


def use_legacy_admission_windows(test_case):
    configure = Scheduler.configure_alternation

    def configure_timed(scheduler, settings, **options):
        legacy = dict(settings)
        legacy.pop("explorer_attempt_limit", None)
        legacy.pop("franta_attempt_limit", None)
        return configure(scheduler, legacy, **options)

    test_case.enterContext(
        mock.patch.object(Scheduler, "configure_alternation", configure_timed)
    )
