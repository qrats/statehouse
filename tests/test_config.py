"""Settings and the jurisdiction registry."""

from __future__ import annotations

import pytest

from statehouse.config.jurisdictions import (
    Jurisdiction,
    JurisdictionRegistry,
    PolitenessPolicy,
    default_registry,
)
from statehouse.config.settings import Settings, load_settings
from statehouse.core.enums import FetchMethod
from statehouse.core.errors import ConfigError, JurisdictionNotConfigured


class TestSettings:
    def test_defaults_are_usable_with_an_empty_environment(self):
        assert load_settings({}).environment == "local"

    def test_string_values_are_read(self):
        assert load_settings({"STATEHOUSE_RAW_BUCKET": "b"}).raw_bucket == "b"

    def test_integers_are_coerced(self):
        assert load_settings({"STATEHOUSE_MAX_RETRIES": "7"}).max_retries == 7

    def test_floats_are_coerced(self):
        assert load_settings({"STATEHOUSE_REQUEST_TIMEOUT_SECONDS": "12.5"}).request_timeout_seconds == 12.5

    @pytest.mark.parametrize("raw,expected", [("true", True), ("0", False), ("YES", True)])
    def test_booleans_are_coerced(self, raw, expected):
        assert load_settings({"STATEHOUSE_RESPECT_ROBOTS": raw}).respect_robots is expected

    def test_a_bad_boolean_is_rejected(self):
        with pytest.raises(ConfigError):
            load_settings({"STATEHOUSE_RESPECT_ROBOTS": "maybe"})

    def test_a_bad_integer_is_rejected(self):
        with pytest.raises(ConfigError):
            load_settings({"STATEHOUSE_MAX_RETRIES": "several"})

    def test_unknown_prefixed_keys_land_in_extras(self):
        assert load_settings({"STATEHOUSE_CUSTOM_THING": "x"}).extras == {"custom_thing": "x"}

    def test_unprefixed_keys_are_ignored(self):
        assert load_settings({"HOME": "/root"}).extras == {}

    def test_log_level_is_upper_cased(self):
        assert load_settings({"STATEHOUSE_LOG_LEVEL": "debug"}).log_level == "DEBUG"

    def test_production_is_recognised(self):
        assert load_settings({"STATEHOUSE_ENVIRONMENT": "production"}).is_production

    def test_index_names_are_prefixed_and_lowercased(self):
        assert Settings(search_index_prefix="SH").index_for("Documents") == "sh-documents"

    def test_a_non_positive_timeout_is_rejected(self):
        with pytest.raises(ConfigError):
            Settings(request_timeout_seconds=0)

    def test_a_non_positive_rate_is_rejected(self):
        with pytest.raises(ConfigError):
            Settings(default_requests_per_minute=0)


class TestPolitenessPolicy:
    def test_the_minimum_interval_follows_the_rate(self):
        assert PolitenessPolicy(requests_per_minute=60).min_interval_seconds == 1.0

    def test_off_hours_raise_the_budget(self):
        policy = PolitenessPolicy(
            requests_per_minute=10, off_hours_multiplier=3.0, off_hours_local=(1, 6)
        )
        assert policy.budget_at(3) == 30

    def test_on_hours_use_the_base_budget(self):
        policy = PolitenessPolicy(
            requests_per_minute=10, off_hours_multiplier=3.0, off_hours_local=(1, 6)
        )
        assert policy.budget_at(14) == 10

    def test_a_window_wrapping_midnight_is_handled(self):
        policy = PolitenessPolicy(
            requests_per_minute=10, off_hours_multiplier=2.0, off_hours_local=(22, 4)
        )
        assert policy.budget_at(23) == 20 and policy.budget_at(2) == 20

    def test_the_window_boundary_is_exclusive_at_the_end(self):
        policy = PolitenessPolicy(
            requests_per_minute=10, off_hours_multiplier=2.0, off_hours_local=(1, 6)
        )
        assert policy.budget_at(6) == 10

    def test_a_non_positive_rate_is_rejected(self):
        with pytest.raises(ConfigError):
            PolitenessPolicy(requests_per_minute=0)

    def test_a_bad_window_is_rejected(self):
        with pytest.raises(ConfigError):
            PolitenessPolicy(off_hours_local=(1, 30))


class TestJurisdiction:
    def test_the_code_is_lowercased(self, sample_jurisdiction):
        assert Jurisdiction(
            code="ZZ",
            name="n",
            timezone="UTC",
            portal_url="https://x.test",
            adapter="a",
        ).code == "zz"

    def test_a_relative_portal_url_is_rejected(self):
        with pytest.raises(ConfigError):
            Jurisdiction(code="zz", name="n", timezone="UTC", portal_url="/x", adapter="a")

    def test_a_biennial_session_anchors_on_the_odd_year(self, sample_jurisdiction):
        assert sample_jurisdiction.session_label(2024) == "2023-2024"

    def test_an_odd_year_anchors_on_itself(self, sample_jurisdiction):
        assert sample_jurisdiction.session_label(2023) == "2023-2024"

    def test_an_annual_session_uses_the_year(self):
        annual = Jurisdiction(
            code="yy",
            name="n",
            timezone="UTC",
            portal_url="https://x.test",
            adapter="a",
            session_pattern="{year}",
        )
        assert annual.session_label(2024) == "2024"

    def test_browser_jurisdictions_report_it(self, registry):
        assert registry["il"].requires_browser

    def test_http_jurisdictions_do_not(self, registry):
        assert not registry["ca"].requires_browser

    def test_with_politeness_returns_a_new_entry(self, sample_jurisdiction):
        adjusted = sample_jurisdiction.with_politeness(requests_per_minute=5)
        assert sample_jurisdiction.politeness.requests_per_minute == 60
        assert adjusted.politeness.requests_per_minute == 5


class TestRegistry:
    def test_lookup_is_case_insensitive(self, registry):
        assert registry["CA"].code == "ca"

    def test_an_unknown_code_raises(self, registry):
        with pytest.raises(JurisdictionNotConfigured):
            registry["zz"]

    def test_duplicate_codes_are_rejected(self, sample_jurisdiction):
        with pytest.raises(ConfigError):
            JurisdictionRegistry([sample_jurisdiction, sample_jurisdiction])

    def test_iteration_is_sorted(self, registry):
        codes = list(registry)
        assert codes == sorted(codes)

    def test_enabled_excludes_disabled_entries(self, registry):
        assert "mo" not in [entry.code for entry in registry.enabled()]

    def test_by_method_filters(self, registry):
        assert all(e.method is FetchMethod.BROWSER for e in registry.by_method("browser"))

    def test_by_tag_filters(self, registry):
        assert all("api" in e.tags for e in registry.by_tag("api"))

    def test_resolve_many_expands_all(self, registry):
        assert len(registry.resolve_many(["all"])) == len(registry.enabled())

    def test_resolve_many_with_no_codes_expands_to_enabled(self, registry):
        assert len(registry.resolve_many([])) == len(registry.enabled())

    def test_resolve_many_resolves_named_codes(self, registry):
        assert [e.code for e in registry.resolve_many(["tx", "ca"])] == ["tx", "ca"]

    def test_replace_swaps_an_entry_without_mutating(self, registry, sample_jurisdiction):
        updated = registry.replace(sample_jurisdiction)
        assert "zz" in updated and "zz" not in registry

    def test_the_default_registry_has_the_federal_source(self, registry):
        assert registry["us"].is_federal

    def test_every_default_entry_has_a_positive_budget(self, registry):
        assert all(registry[c].politeness.requests_per_minute > 0 for c in registry)

    def test_the_default_registry_covers_every_fetch_method(self, registry):
        methods = {registry[c].method for c in registry}
        assert {FetchMethod.HTTP, FetchMethod.API, FetchMethod.BROWSER, FetchMethod.BULK} <= methods

    def test_default_registry_is_not_shared_state(self):
        assert default_registry() is not default_registry()
