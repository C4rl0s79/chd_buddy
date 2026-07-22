"""Lokalizacja UI: tr() tłumaczy na EN, PL to passthrough, brak wpisu = fallback."""
from __future__ import annotations

from chd_buddy.core import i18n
from chd_buddy.core.settings import Settings


def teardown_function(_):
    i18n.set_language("pl")          # nie przeciekaj stanu między testami


def test_pl_is_passthrough():
    i18n.set_language("pl")
    assert i18n.get_language() == "pl"
    assert i18n.tr("nieznane → ToSort") == "nieznane → ToSort"
    assert i18n.tr("cokolwiek nieznanego") == "cokolwiek nieznanego"


def test_en_translates_known_and_falls_back():
    i18n.set_language("en")
    assert i18n.get_language() == "en"
    assert i18n.tr("nieznane → ToSort") == "unknown → ToSort"
    assert i18n.tr("🔍 Skanuj i raportuj") == "🔍 Scan and report"
    # brak wpisu => zwraca oryginał (polski), nie wywala się
    assert i18n.tr("Tekst bez tłumaczenia") == "Tekst bez tłumaczenia"


def test_set_language_normalizes():
    i18n.set_language("English")
    assert i18n.get_language() == "en"
    i18n.set_language("pl_PL")
    assert i18n.get_language() == "pl"
    i18n.set_language(None)
    assert i18n.get_language() == "pl"


def test_language_setting_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "path", classmethod(
        lambda cls: tmp_path / "settings.json"))
    s = Settings()
    assert s.language == "pl"
    s.language = "en"
    s.save()
    assert Settings.load().language == "en"


def test_languages_catalog():
    assert set(i18n.LANGUAGES) >= {"pl", "en"}


def test_en_covers_representative_ui():
    """Reprezentatywny przekrój UI (zakładki, dialogi, komunikaty, tooltipy)
    ma tłumaczenie EN — stróż przed regresją słownika."""
    i18n.set_language("en")
    sample = [
        # zakładki / menu
        "📋 Wczytaj DAT-y", "👥 Pokaż duplikaty", "📦 Skanuj i instaluj do emulatorów",
        "🔎 Sprawdź wersje", "Język / Language…",
        # okna dialogowe
        "Ustawienia DAT-a:", "Ustawienia katalogu:",
        "Zależności DAT-ów — rodzic → dzieci (per platforma)",
        # komunikaty
        "Napraw kolekcję", "Pełny skan", "Wskaż istniejący katalog DAT-ów.",
        "Brak działającego chdman",
        # tooltip / filtr
        "Sortuj gry:", "✅ tylko komplet",
        # klasyczne narzędzie
        "Audyt (wykryj złe CHD)",
    ]
    missing = [k for k in sample if i18n.tr(k) == k]
    assert not missing, f"brak tłumaczenia EN dla: {missing}"


def test_en_values_nonempty_and_dict_sane():
    assert all(v for v in i18n._EN.values())      # brak pustych tłumaczeń
    assert len(i18n._EN) > 300
