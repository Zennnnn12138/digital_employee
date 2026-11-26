from app.eval.runner import run_suite


async def test_sixty_task_regression():
    report = await run_suite()
    assert report["total"] == 60
    assert report["failures"] == [], json_failures(report)
    assert report["pass_rate"] >= 0.85
    assert report["normal_delivery_rate"] >= 0.85
    assert report["unauthorized_blocked"] is True
    assert report["duplicate_safe"] is True


def json_failures(report: dict) -> str:
    return "\n".join(f"{item['id']}: {item['detail']}" for item in report["failures"])
