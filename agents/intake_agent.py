import pandas as pd
import anthropic
import json
import os

_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), '..', 'column_overrides.json')

def _load_overrides() -> dict:
    if not os.path.exists(_OVERRIDES_PATH):
        return {}
    with open(_OVERRIDES_PATH, 'r') as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith('_')}

def _apply_overrides(df: pd.DataFrame, path: str) -> pd.DataFrame:
    overrides = _load_overrides()
    path_lower = path.lower()
    for key, mapping in overrides.items():
        if key.lower() in path_lower:
            df = df.rename(columns={src: dst for src, dst in mapping.items() if src in df.columns})
            break
    return df

def detect_column(df, keywords):
    for col in df.columns:
        if any(kw in col.lower() for kw in keywords):
            return col
    return None

def run_intake(filepaths: list[str]) -> dict:
    accounts = {
        "checking": None,
        "credit_card": None,
        "investments": None
    }

    for path in filepaths:
        try:
            df = pd.read_csv(path, skiprows=0)
        except Exception as e:
            raise ValueError(f"Could not read '{path}': {e}")

        df.columns = df.columns.str.strip().str.replace('\ufeff', '')

        # Apply manual overrides before fuzzy matching
        df = _apply_overrides(df, path)

        col_map = {
            'Date':        ['date', 'trans', 'post', 'transaction', 'time'],
            'Amount':      ['amount', 'sum', 'total', 'debit', 'credit', 'charge'],
            'Type':        ['type', 'transaction type', 'dr/cr'],
            'Category':    ['category', 'cat', 'group', 'label'],
            'Account':     ['account', 'acct', 'bank'],
            'Description': ['description', 'desc', 'memo', 'narrative', 'details']
        }

        for standard_name, keywords in col_map.items():
            if standard_name not in df.columns:
                match = detect_column(df, keywords)
                if match:
                    df = df.rename(columns={match: standard_name})

        if 'Amount' not in df.columns:
            raise ValueError(
                f"Could not find an amount/charge column in '{path}'. "
                f"Columns found: {list(df.columns)}. "
                "Try renaming your amount column to 'Amount'."
            )

        df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df = df.dropna(subset=['Amount', 'Date'])
        else:
            df = df.dropna(subset=['Amount'])

        if df.empty:
            raise ValueError(f"No valid rows found in '{path}' after parsing. Check that dates and amounts are formatted correctly.")

        if 'Type' not in df.columns:
            df['Type'] = df['Amount'].apply(lambda x: 'Credit' if x < 0 else 'Debit')
            df['Amount'] = df['Amount'].abs()

        path_lower = path.lower()
        account_types = df['Account'].str.upper().unique() if 'Account' in df.columns else []

        if any(k in path_lower for k in ['discover', 'credit', 'card', 'visa', 'mastercard', 'amex']):
            accounts['credit_card'] = df
        elif any(k in path_lower for k in ['invest', 'schwab', 'brokerage', 'fidelity', 'vanguard', 'robinhood']):
            accounts['investments'] = df
        elif any('checking' in a.lower() for a in account_types) or any(k in path_lower for k in ['checking', 'transactions']):
            accounts['checking'] = df
        else:
            if 'Type' in df.columns:
                accounts['checking'] = df
            else:
                accounts['credit_card'] = df

    summary = {}

    date_min = None
    date_max = None

    def update_date_range(df):
        nonlocal date_min, date_max
        if 'Date' in df.columns:
            mn = df['Date'].min()
            mx = df['Date'].max()
            if pd.notna(mn):
                date_min = mn if date_min is None else min(date_min, mn)
            if pd.notna(mx):
                date_max = mx if date_max is None else max(date_max, mx)

    if accounts['checking'] is not None:
        df = accounts['checking']
        credits = df[df['Type'] == 'Credit']['Amount'].sum()
        debits  = df[df['Type'] == 'Debit']['Amount'].sum()
        summary['checking_inflow']  = round(credits, 2)
        summary['checking_outflow'] = round(debits, 2)
        summary['checking_net']     = round(credits - debits, 2)
        summary['checking_df']      = accounts['checking']
        update_date_range(df)

    if accounts['credit_card'] is not None:
        df = accounts['credit_card']
        summary['credit_card_balance'] = round(df['Amount'].sum(), 2)
        summary['credit_card_df']      = accounts['credit_card']
        update_date_range(df)

    if accounts['investments'] is not None:
        summary['investments_df'] = accounts['investments']
        update_date_range(accounts['investments'])

    if date_min is not None and date_max is not None:
        summary['date_range'] = {
            'start': date_min.strftime('%b %d, %Y'),
            'end':   date_max.strftime('%b %d, %Y')
        }

    return summary


def generate_questions(summary: dict) -> list[dict]:
    """
    Takes the parsed financial summary and asks Claude to generate
    smart clarifying questions based on what it actually sees in the data.
    Returns a list of question dicts: [{id, question, type, options?}]
    """
    client = anthropic.Anthropic()

    # Build a plain-text snapshot of what was detected
    data_snapshot = ""

    if 'checking_df' in summary:
        df = summary['checking_df']
        cats = []
        if 'Category' in df.columns:
            cats = df[df['Type'] == 'Debit'].groupby('Category')['Amount'].sum().sort_values(ascending=False).head(8).round(2).to_dict()
        descriptions = df['Description'].dropna().unique().tolist()[:20] if 'Description' in df.columns else []
        data_snapshot += f"CHECKING ACCOUNT DETECTED\n"
        data_snapshot += f"Inflow: ${summary['checking_inflow']} | Outflow: ${summary['checking_outflow']} | Net: ${summary['checking_net']}\n"
        if cats:
            data_snapshot += f"Top spending categories: {json.dumps(cats)}\n"
        if descriptions:
            data_snapshot += f"Sample transaction descriptions: {descriptions}\n"

    if 'credit_card_df' in summary:
        cc_df = summary['credit_card_df']
        cc_cats = []
        if 'Category' in cc_df.columns:
            cc_cats = cc_df.groupby('Category')['Amount'].sum().sort_values(ascending=False).head(6).round(2).to_dict()
        data_snapshot += f"\nCREDIT CARD DETECTED\n"
        data_snapshot += f"Total charged: ${summary['credit_card_balance']}\n"
        if cc_cats:
            data_snapshot += f"Credit card categories: {json.dumps(cc_cats)}\n"

    if 'investments_df' in summary:
        data_snapshot += "\nINVESTMENT ACCOUNT DETECTED\n"

    prompt = f"""You are a smart financial intake agent. A user just uploaded their bank statements. 
Based on what you see in the data below, generate 4-5 clarifying questions that will meaningfully improve the quality of the financial analysis.

Focus on things you genuinely CANNOT infer from the data:
- Financial goals (the data can't tell you these)
- Whether specific large/recurring transactions are transfers vs real spending (flag ones you spotted)
- Context behind unusual patterns you noticed
- Time horizon and priorities

Do NOT ask about things already visible in the data.
Be specific — reference actual amounts or merchants you see when relevant.

Data snapshot:
{data_snapshot}

Return a JSON array of question objects. Each object must have:
- "id": short snake_case identifier (e.g. "primary_goal")
- "question": the question text shown to user
- "type": either "text", "select", or "multiselect"  
- "options": array of strings (only include if type is select or multiselect)

Return only valid JSON. No explanation outside the JSON array."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        questions = json.loads(raw)
    except json.JSONDecodeError:
        questions = [{"id": "general_goals", "question": "What are your main financial goals right now?", "type": "text"}]
    return questions
