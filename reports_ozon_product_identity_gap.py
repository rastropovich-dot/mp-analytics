import argparse
import json

from loaders.ozon_product_identity_loader import DEFAULT_MARKETPLACE, build_plan, latest_issue_date, parse_include_api_sources


def parse_args():
    parser = argparse.ArgumentParser(description="DB-only report for Ozon product identity gaps.")
    parser.add_argument("--issue-date")
    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--limit-tail", type=int)
    parser.add_argument("--include-api-sources", default="product-list,info-list,attributes,stocks")
    parser.add_argument("--limit-output", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run():
    args = parse_args()
    issue_date = args.issue_date or latest_issue_date(args.marketplace)
    if not issue_date:
        raise RuntimeError("No stock_data_quality_issues rows found for requested marketplace.")

    plan = build_plan(
        issue_date=issue_date,
        marketplace_code=args.marketplace,
        only_tail=True,
        limit_tail=args.limit_tail,
        include_api_sources=parse_include_api_sources(args.include_api_sources),
    )

    report = {
        "issue_date": issue_date,
        "marketplace_code": args.marketplace,
        "summary": {
            "total_tail": plan["total_tail"],
            "decision_sku_not_mapped": plan["decision_sku_not_mapped"],
            "article_not_in_stock_source": plan["article_not_in_stock_source"],
            "truly_unrecoverable_from_current_db": plan["truly_unrecoverable_from_current_db"],
            "recoverable_via_promoted_sku": plan["recoverable_via_promoted_sku"],
            "recovered_article_but_no_stock_source": plan["recovered_article_but_no_stock_source"],
            "requires_stock_source_verification": plan["requires_stock_source_verification"],
        },
        "rows": plan["tail_rows"][: args.limit_output],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
