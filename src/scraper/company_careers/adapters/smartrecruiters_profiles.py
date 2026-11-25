"""Review-bound extensions to the standard SmartRecruiters listing limits."""


def extended_listing_bounds(target, identifier):
    return (
        target.name == "Bosch" and identifier == "BoschGroup"
        and target.source_scope == "default" and target.identity_regex == "^Bosch Group$"
        and target.endpoint_url == "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
        and target.careers_url == "https://jobs.bosch.com/en/"
        and not target.selective_detail_enrichment and target.page_size == 100
        and (target.max_pages, target.max_jobs) == (65, 6500)
    )
