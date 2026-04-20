import anthropic
import json

def run_analysis(summary: dict, profile_context: str = "") -> dict:
    client = anthropic.Anthropic()

    data_context = ""

    if 'checking_df' in summary:
        df = summary['checking_df']
        category_spending = (
            df[df['Type'] == 'Debit']
            .groupby('Category')['Amount']
            .sum()
            .sort_values(ascending=False)
            .round(2)
            .to_dict()
        ) if 'Category' in df.columns else {}

        data_context += "CHECKING ACCOUNT\n"
        data_context += f"Total Inflow: ${summary.get('checking_inflow', 0)}\n"
        data_context += f"Total Outflow: ${summary.get('checking_outflow', 0)}\n"
        data_context += f"Net: ${summary.get('checking_net', 0)}\n"
        if category_spending:
            data_context += f"Spending by Category: {json.dumps(category_spending, indent=2)}\n\n"

    if 'credit_card_df' in summary:
        df = summary['credit_card_df']
        cc_cats = {}
        if 'Category' in df.columns:
            cc_cats = (
                df.groupby('Category')['Amount']
                .sum()
                .sort_values(ascending=False)
                .round(2)
                .to_dict()
            )
        data_context += "CREDIT CARD\n"
        data_context += f"Total Charged: ${summary.get('credit_card_balance', 0)}\n"
        if cc_cats:
            data_context += f"Spending by Category: {json.dumps(cc_cats, indent=2)}\n\n"

    if 'investments_df' in summary:
        data_context += "INVESTMENT ACCOUNT DETECTED\n\n"

    # Inject user profile if it exists
    profile_section = ""
    if profile_context:
        profile_section = f"\n{profile_context}\n\nUse the user's stated goals and context to personalize every section of the analysis.\n"

    prompt = f"""You are a financial analysis agent. Analyze this transaction data and return a JSON object with exactly these four keys:

"net_worth": A clear net worth snapshot string. Use: checking net + investments - credit card balance. State what data is present and estimate conservatively if anything is missing.

"budget": An object with:
  - "categories": array of objects, one per spending category, each with:
      - "category": string name
      - "amount": number (dollars)
      - "percentage": number (% of total outflow, as a plain number like 23.4)
      - "assessment": one sentence on whether this seems healthy or concerning
  - "summary": 2-3 sentence overall budget summary
  - "total_outflow": total outflow number

"investments": A string analyzing investment activity, consistency, and whether the pace aligns with wealth-building goals.

"actions": Array of objects, each with:
  - "priority": number (1 = highest)
  - "action": string — specific, direct, no fluff

{profile_section}

Financial data:
{data_context}

Return only valid JSON. No explanation outside the JSON."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "net_worth": "Analysis could not be parsed. Please try again.",
            "budget": {"categories": [], "summary": "Unavailable.", "total_outflow": 0},
            "investments": "Unavailable.",
            "actions": [{"priority": 1, "action": "Re-upload your statements and try again."}]
        }
    return result
