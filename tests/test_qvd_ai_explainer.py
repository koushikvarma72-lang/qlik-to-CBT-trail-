from qvd_business_analysis.llm_explainer import _parse_ai_response


def test_parse_ai_response_accepts_fenced_json_and_metric_object():
    context = {
        "source": {},
        "entity_summary": {},
        "kpis": [],
        "dimensions": [],
    }
    text = """```json
{
  "summary_markdown": "This analysis examines a sales dataset.",
  "metric_explanations": {
    "Actual Sales": "Total sales revenue - sums all sales transactions",
    "Budget vs Actual": "Compares planned sales to what actually occurred"
  },
  "migration_narrative": "Review the source, then the validated metrics."
}
```"""

    summary, metric_explanations, narrative, warnings = _parse_ai_response(text, context)

    assert summary == "This analysis examines a sales dataset."
    assert narrative == "Review the source, then the validated metrics."
    assert warnings == []
    assert metric_explanations == [
        {
            "metric_name": "Actual Sales",
            "plain_english": "Total sales revenue - sums all sales transactions",
            "source_columns": [],
            "analyze_by": [],
            "date_column": "",
        },
        {
            "metric_name": "Budget vs Actual",
            "plain_english": "Compares planned sales to what actually occurred",
            "source_columns": [],
            "analyze_by": [],
            "date_column": "",
        },
    ]


def test_parse_ai_response_accepts_blank_lines_inside_json_fence():
    context = {
        "source": {},
        "entity_summary": {},
        "kpis": [],
        "dimensions": [],
    }
    text = """```json

{

"summary_markdown": "This analysis examines a sales dataset containing 84,775 records with 32 fields.",

"metric_explanations": {

"Actual Sales": "The real sales amounts achieved, used to measure true business performance.",

"Budget Sales": "Planned sales targets, used to compare actual performance against expectations."

},

"migration_narrative": "This sales data can drive business insights."

}

```"""

    summary, metric_explanations, narrative, warnings = _parse_ai_response(text, context)

    assert summary.startswith("This analysis examines a sales dataset")
    assert narrative.startswith("This sales data")
    assert warnings == []
    assert metric_explanations[0]["metric_name"] == "Actual Sales"
    assert metric_explanations[1]["plain_english"].startswith("Planned sales targets")
