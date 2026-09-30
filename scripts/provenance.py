"""Field provenance (Step U post-live calibration): where every critical metric came from.

Each entry:
  value, provider, source_field, scope, period, provider_observation_timestamp, retrieved_at,
  availability  AVAILABLE | MISSING | DERIVED | NOT_COMPARABLE
  quality       PROVIDER | CALCULATED | UNVERIFIED | NOT_COMPARABLE | N/A
Scores consume normalized values; the record keeps this traceable path next to them.
"""

PROVIDER = "kalopilot"


def entry(value, source_field, scope="PRODUCT", period="30d", provider_ts=None, retrieved_at=None,
          availability=None, quality=None, provider=PROVIDER, **extra):
    has = value is not None and not (isinstance(value, list) and not [x for x in value if x is not None])
    return {"value": value, "provider": provider, "source_field": source_field, "scope": scope, "period": period,
            "provider_observation_timestamp": provider_ts, "retrieved_at": retrieved_at,
            "availability": availability or ("AVAILABLE" if has else "MISSING"),
            "quality": quality or ("PROVIDER" if has else "N/A"), **extra}


def deep_provenance(f, provider_ts, retrieved_at, comp):
    """f = normalized deep facts; comp = competition scope dict."""
    def e(value, src, **kw):
        return entry(value, src, provider_ts=provider_ts, retrieved_at=retrieved_at, **kw)
    pmin, pmax = f.get("price_min"), f.get("price_max")
    avg = (pmin + pmax) / 2 if None not in (pmin, pmax) else None
    gs = f.get("growth_source")
    growth_quality = {"calculated": "CALCULATED", "provider_unverified": "UNVERIFIED",
                      "no_previous_revenue": "N/A"}.get(gs)
    return {
        "price_min": e(pmin, "price_min"), "price_max": e(pmax, "price_max"),
        "price_avg": e(avg, "price_min+price_max", availability="DERIVED" if avg is not None else "MISSING",
                       quality="CALCULATED" if avg is not None else "N/A"),
        "gmv_30d": e(f.get("gmv_30d"), "gmv_30d"),
        "gmv_prev_30d": e(f.get("gmv_prev_30d"), "gmv_prev_30d", period="previous 30d"),
        "units_30d": e(f.get("units_30d"), "units_30d"),
        "growth_30d": e(f.get("growth_30d_pct"),
                        "gmv_30d,gmv_prev_30d" if gs == "calculated" else "growth_30d_pct",
                        period="30d vs previous 30d",
                        availability="DERIVED" if gs == "calculated" else None, quality=growth_quality,
                        provider_value=f.get("growth_30d_provider")),
        "daily_gmv": e(f.get("daily_gmv"), "daily_gmv", period="daily, 30d"),
        "creator_count": e(f.get("creator_count"), "creator_count"),
        "selling_creator_count": e(f.get("selling_creator_count"), "selling_creator_count"),
        "top_creators": e(f.get("top_creators"), "top_creators"),
        "creator_growth_pct": e(f.get("creator_growth_pct"), "creator_growth_pct"),
        "video_count": e(f.get("video_count"), "video_count"),
        "selling_video_count": e(f.get("selling_video_count"), "selling_video_count"),
        "video_sales_share_pct": e(f.get("video_sales_share_pct"), "video_sales_share_pct"),
        "top_videos": e(f.get("top_videos"), "top_videos"),
        "video_growth_pct": e(f.get("video_growth_pct"), "video_growth_pct"),
        "category_growth_pct": e(f.get("category_growth_pct"), "category_growth_pct", scope="CATEGORY (as returned)"),
        "competition_count": e(comp["competition_count"], comp["source_field"], scope=comp["competition_scope"],
                               availability=("NOT_COMPARABLE" if comp["competition_count"] is not None
                                             and not comp["competition_comparable"] else None),
                               quality="NOT_COMPARABLE" if comp["competition_count"] is not None
                               and not comp["competition_comparable"] else None),
        "shop_count": e(f.get("shop_count"), "shop_count"),
        "commission_pct": e(f.get("commission_pct"), "commission_pct"),
        "similar_listings_count": e(f.get("similar_listings_count"), "similar_listings_count",
                                    scope="PRODUCT_CLUSTER"),
    }


def discovery_provenance(facts, source):
    pts, ret = facts.get("data_window_end"), source.get("fetched_at")
    gs = facts.get("growth_source")
    return {k: entry(facts.get(k), src, provider_ts=pts, retrieved_at=ret,
                     **({"availability": "DERIVED", "quality": "CALCULATED"} if k == "growth_30d" and gs == "calculated"
                        else {"quality": "UNVERIFIED"} if k == "growth_30d" and gs == "provider_unverified" else {}))
            for k, src in (("price_min", "price_min"), ("price_max", "price_max"), ("gmv_30d", "gmv_30d"),
                           ("gmv_prev_30d", "gmv_prev_30d"), ("units_30d", "units_30d"),
                           ("growth_30d", "growth_30d_pct"), ("creator_count", "creator_count"),
                           ("video_count", "video_count"), ("shop_count", "shop_count"))}


# WPS / Confidence input name -> provenance key (traceable path from a score to its source)
WPS_INPUT_PROVENANCE = {
    "revenue_growth_pct": "growth_30d", "recent_velocity_pct": "daily_gmv", "acceleration_pp": "daily_gmv",
    "units_sold": "units_30d", "videos_count": "video_count", "video_sales_share_pct": "video_sales_share_pct",
    "creators_count": "creator_count", "top_creators_growth_pct": "top_creators",
    "category_growth_pct": "category_growth_pct", "competition_count_comparable": "competition_count",
    "price_avg": "price_avg", "commission_pct": "commission_pct", "daily_sales_series": "daily_gmv", "gmv": "gmv_30d",
    "shops_count": "shop_count", "similar_listings_count": "similar_listings_count",
}
