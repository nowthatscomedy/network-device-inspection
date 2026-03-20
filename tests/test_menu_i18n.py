from __future__ import annotations

from core import menu_i18n
from core.i18n import set_locale


def test_show_action_menu_toggles_selection_with_select(monkeypatch) -> None:
    set_locale("en", "en")

    responses = iter(["backup", "inspection", "__done__"])
    captured_calls: list[dict[str, object]] = []

    class Prompt:
        def __init__(self, value: object) -> None:
            self.value = value

        def execute(self) -> object:
            return self.value

    def fake_select(**kwargs):
        captured_calls.append(kwargs)
        return Prompt(next(responses))

    monkeypatch.setattr(menu_i18n.inquirer, "select", fake_select)
    monkeypatch.setattr(
        menu_i18n.inquirer,
        "checkbox",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("checkbox should not be used")),
    )

    result = menu_i18n.show_action_menu()

    assert result == ["inspection", "backup"]
    assert captured_calls
    assert captured_calls[0]["instruction"] == "Enter: toggle"


def test_show_action_menu_requires_selection_before_done(monkeypatch) -> None:
    set_locale("en", "en")

    responses = iter(["__done__", "custom_commands", "__done__"])
    captured_calls: list[dict[str, object]] = []

    class Prompt:
        def __init__(self, value: object) -> None:
            self.value = value

        def execute(self) -> object:
            return self.value

    def fake_select(**kwargs):
        captured_calls.append(kwargs)
        return Prompt(next(responses))

    monkeypatch.setattr(menu_i18n.inquirer, "select", fake_select)

    result = menu_i18n.show_action_menu()

    assert result == ["custom_commands"]
    assert len(captured_calls) == 3


def test_show_action_order_menu_includes_back_choice(monkeypatch) -> None:
    set_locale("en", "en")

    captured: dict[str, object] = {}

    class Prompt:
        def execute(self) -> object:
            return None

    def fake_select(**kwargs):
        captured.update(kwargs)
        return Prompt()

    monkeypatch.setattr(menu_i18n.inquirer, "select", fake_select)

    result = menu_i18n.show_action_order_menu(
        [("Inspection -> Backup", ["inspection", "backup"])],
        default_label="Inspection -> Backup",
    )

    assert result is None
    choices = captured["choices"]
    assert any(
        isinstance(choice, dict) and choice.get("value") is None
        for choice in choices
    )
