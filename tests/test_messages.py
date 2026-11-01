from wt.messages import CONFIRM_REMOVE, NO_SERVICES_YET, no_services_message


def test_confirm_remove_sentence() -> None:
    assert CONFIRM_REMOVE.format(branch="feat-a") == (
        "remove feat-a: removes what wt created for it (containers, volumes, databases, "
        "worktrees) and deletes the git branch in every repo of the project where it exists; "
        "unpushed commits are lost. Remove? y/N"
    )


def test_no_services_template() -> None:
    assert NO_SERVICES_YET == (
        "branch {branch} has no services yet; run wt add {branch} --uses {example} "
        "(services: {names})"
    )


def test_no_services_message_uses_the_first_service_as_example() -> None:
    assert no_services_message("feat-a", ["api", "web"]) == (
        "branch feat-a has no services yet; run wt add feat-a --uses api (services: api, web)"
    )


def test_no_services_message_with_ten_names_lists_all() -> None:
    names = [f"s{i}" for i in range(10)]
    message = no_services_message("b", names)
    assert message.endswith("(services: " + ", ".join(names) + ")")
    assert "…" not in message


def test_no_services_message_cuts_after_ten_names() -> None:
    names = [f"s{i}" for i in range(11)]
    message = no_services_message("b", names)
    assert message.endswith("(services: " + ", ".join(names[:10]) + ", …)")
    assert "s10" not in message
