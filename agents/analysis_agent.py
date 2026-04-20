import anthropic
import json

def run_analysis(summary: dict, profile_context: str = "") -> dict:
    client = anthropic.Anthropic()

    data_context = ""

    for acct_type, label in [('checking', 'CHECKING'), ('savings', 'SAVINGS')]:
        if f'{acct_type}_df' in summary:
            df = summary[f'{acct_type}_df']
            cat_spending = (
                df[df['Type'] == 'Debit']
                .groupby('Category')['Amount']
                .sum()
                .sort_values(ascending=False)
                .round(2)
                .to_dict()
            ) if 'Category' in df.columns else {}
            data_context += f"{label} ACCOUNT\n"
            data_context += f"Inflow: ${summary.get(f'{acct_type}_inflow', 0)}\n"
            data_context += f"Outflow: ${summary.get(f'{acct_type}_outflow', 0)}\n"
            data_context += f"Net: ${summary.get(f'{acct_type}_net', 0)}\n"
            if cat_spending:
                data_context += f"Spending by Category: {json.dumps(cat_spending, indent=2)}\n\n"

    if 'credit_card_df' in summary:
        df = summary['credit_card_df']
        cc_cats = (
            df[df['Type'] == 'Debit']
            .groupby('Category')['Amount']
            .sum()
            .sort_values(ascending=False)
            .round(2)
            .to_dict()
        ) if 'Category' in df.columns else {}
        data_context += "CREDIT CARD\n"
        data_context += f"Charges this period: ${summary.get('credit_card_charges', 0)}\n"
        data_context += f"Payments made: ${summary.get('credit_card_payments', 0)}\n"
        data_context += f"Net spend (charges minus payments): ${summary.get('credit_card_net_spend', 0)}\n"
        data_context += "Note: actual current balance is unknown — user should confirm separately.\n"
        if cc_cats:
            data_context += f"Spending by Category: {json.dumps(cc_cats, indent=2)}\n\n"

    if 'investments_df' in summary:
        inv_df = summary['investments_df']
        data_context += "INVESTMENT ACCOUNT\n"

        # Total contributions vs withdrawals
        if 'Type' in inv_df.columns:
            contributions = inv_df[inv_df['Type'] == 'Debit']['Amount'].sum()
            withdrawals   = inv_df[inv_df['Type'] == 'Credit']['Amount'].sum()
            data_context += f"Contributions: ${round(contributions, 2)}\n"
            data_context += f"Withdrawals: ${round(withdrawals, 2)}\n"
            data_context += f"Net invested: ${round(contributions - withdrawals, 2)}\n"
        else:
            net = round(inv_df['Amount'].sum(), 2)
            data_context += f"Net activity: ${net}\n"

        # Top holdings or transaction descriptions
        if 'Description' in inv_df.columns:
            top = inv_df['Description'].dropna().value_counts().head(8).to_dict()
            data_context += f"Top holdings/transactions: {json.dumps(top)}\n"

        # Category breakdown if available
        if 'Category' in inv_df.columns:
            inv_cats = (
                inv_df.groupby('Category')['Amount']
                .sum()
                .sort_values(ascending=False)
                .round(2)
                .to_dict()
            )
            data_context += f"By category: {json.dumps(inv_cats)}\n"

        data_context += "\n"

    # Build monthly trend data for charts (passed to frontend, not Claude)
    chart_data = {}
    for key, acct_key in [('checking_df', 'checking'), ('savings_df', 'savings'), ('credit_card_df', 'credit_card')]:
        if key in summary:
            df = summary[key]
            if 'Date' in df.columns:
                df = df.copy()
                df['Month'] = df['Date'].dt.to_period('M').astype(str)
                monthly = (
                    df[df['Type'] == 'Debit']
                    .groupby('Month')['Amount']
                    .sum()
                    .round(2)
                    .to_dict()
                )
                chart_data[acct_key + '_monthly'] = monthly

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

"investments": A string analyzing: total contributions vs withdrawals, consistency of investing behavior, what's being invested in (if visible), whether the pace aligns with wealth-building goals, and one specific suggestion. If no investment data was provided, note that and recommend next steps.

"actions": Array of objects, each with:
  - "priority": number (1 = highest)
  - "action": string — specific, direct, no fluff

{profile_section}

Financial data:
{data_context}

Return only valid JSON. No explanation outside the JSON."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
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

    result["chart_data"] = chart_data
    return result
