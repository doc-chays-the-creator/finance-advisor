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


def _apply_overrides(df, path):
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


def _parse_amount(series):
    """Parse amounts that may have comma thousands separators like -2,466.26."""
    return pd.to_numeric(
        series.astype(str).str.replace(',', '', regex=False),
        errors='coerce'
    )


def _normalize_type(series):
    """Normalize Deposit/Withdrawal/Purchase etc. to Credit/Debit."""
    mapping = {
        'deposit':    'Credit',
        'credit':     'Credit',
        'debit':      'Debit',
        'withdrawal': 'Debit',
        'withdraw':   'Debit',
        'payment':    'Debit',
        'purchase':   'Debit',
        'sale':       'Debit',
    }
    normalized = series.str.strip().str.lower().map(mapping)
    return normalized.fillna('Debit')


def parse_account(path: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, skiprows=0)
    except Exception as e:
        raise ValueError(f"Could not read '{path}': {e}")

    df.columns = df.columns.str.strip().str.replace('\ufeff', '', regex=False)
    df = _apply_overrides(df, path)

    # Detect split Amount Debit / Amount Credit columns (e.g. Associated CU checking)
    debit_col  = detect_column(df, ['amount debit', 'debit amount'])
    credit_col = detect_column(df, ['amount credit', 'credit amount'])

    if debit_col and credit_col:
        debits  = _parse_amount(df[debit_col]).abs()
        credits = _parse_amount(df[credit_col]).abs()
        df['Amount'] = debits.combine_first(credits)
        df['Type']   = 'Debit'
        df.loc[credits.notna() & debits.isna(), 'Type'] = 'Credit'
        df = df.drop(columns=[debit_col, credit_col], errors='ignore')
    else:
        col_map = {
            'Date':        ['date', 'trans', 'post', 'transaction'],
            'Amount':      ['amount', 'sum', 'total', 'charge'],
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
            f"Could not find an amount column in '{path}'. "
            f"Columns found: {list(df.columns)}."
        )

    df['Amount'] = _parse_amount(df['Amount'])

    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        df = df.dropna(subset=['Amount', 'Date'])
    else:
        df = df.dropna(subset=['Amount'])

    # Drop footer/metadata rows (Amount couldn't be parsed = NaN, already dropped above)
    df = df[df['Amount'].notna()].copy()

    if df.empty:
        raise ValueError(f"No valid rows found in '{path}' after parsing.")

    # Normalize type labels (Deposit -> Credit, Withdrawal -> Debit, etc.)
    if 'Type' in df.columns:
        df['Type'] = _normalize_type(df['Type'])
        df['Amount'] = df['Amount'].abs()
    else:
        # Signed amount convention: negative = outflow
        df['Type']   = df['Amount'].apply(lambda x: 'Credit' if x < 0 else 'Debit')
        df['Amount'] = df['Amount'].abs()

    return df


def run_intake(file_map: dict) -> dict:
    """
    file_map: {'checking': path, 'savings': path, 'credit_card': path, 'investments': path}
    Account type is explicit -- no filename guessing.
    """
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

    for acct_type in ['checking', 'savings']:
        if acct_type in file_map:
            df = parse_account(file_map[acct_type])
            credits = df[df['Type'] == 'Credit']['Amount'].sum()
            debits  = df[df['Type'] == 'Debit']['Amount'].sum()
            summary[f'{acct_type}_inflow']  = round(credits, 2)
            summary[f'{acct_type}_outflow'] = round(debits, 2)
            summary[f'{acct_type}_net']     = round(credits - debits, 2)
            summary[f'{acct_type}_df']      = df
            update_date_range(df)

    if 'credit_card' in file_map:
        df = parse_account(file_map['credit_card'])
        charges  = df[df['Type'] == 'Debit']['Amount'].sum()
        payments = df[df['Type'] == 'Credit']['Amount'].sum()
        summary['credit_card_charges']   = round(charges, 2)
        summary['credit_card_payments']  = round(payments, 2)
        summary['credit_card_net_spend'] = round(charges - payments, 2)
        summary['credit_card_df']        = df
        update_date_range(df)

    if 'investments' in file_map:
        df = parse_account(file_map['investments'])
        summary['investments_df'] = df
        update_date_range(df)

    if date_min is not None and date_max is not None:
        summary['date_range'] = {
            'start': date_min.strftime('%b %d, %Y'),
            'end':   date_max.strftime('%b %d, %Y')
        }

    return summary


def generate_questions(summary: dict) -> list:
    client = anthropic.Anthropic()
    data_snapshot = ""

    for acct_type, label in [('checking', 'CHECKING'), ('savings', 'SAVINGS')]:
        if f'{acct_type}_df' in summary:
            df = summary[f'{acct_type}_df']
            cats = {}
            if 'Category' in df.columns:
                cats = df[df['Type'] == 'Debit'].groupby('Category')['Amount'].sum().sort_values(ascending=False).head(8).round(2).to_dict()
            descriptions = df['Description'].dropna().unique().tolist()[:15] if 'Description' in df.columns else []
            data_snapshot += f"{label} ACCOUNT\n"
            data_snapshot += f"Inflow: ${summary[f'{acct_type}_inflow']} | Outflow: ${summary[f'{acct_type}_outflow']} | Net: ${summary[f'{acct_type}_net']}\n"
            if cats:
                data_snapshot += f"Top categories: {json.dumps(cats)}\n"
            if descriptions:
                data_snapshot += f"Sample descriptions: {descriptions}\n"

    if 'credit_card_df' in summary:
        cc_df = summary['credit_card_df']
        cc_cats = {}
        if 'Category' in cc_df.columns:
            cc_cats = cc_df[cc_df['Type'] == 'Debit'].groupby('Category')['Amount'].sum().sort_values(ascending=False).head(6).round(2).to_dict()
        data_snapshot += f"\nCREDIT CARD\n"
        data_snapshot += f"Charges: ${summary['credit_card_charges']} | Payments: ${summary['credit_card_payments']} | Net spend: ${summary['credit_card_net_spend']}\n"
        if cc_cats:
            data_snapshot += f"Top categories: {json.dumps(cc_cats)}\n"

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
- "id": short snake_case identifier
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
        return json.loads(raw)
    except json.JSONDecodeError:
        return [{"id": "general_goals", "question": "What are your main financial goals right now?", "type": "text"}]
