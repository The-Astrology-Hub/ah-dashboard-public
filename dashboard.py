import pandas as pd
import streamlit as st
from supabase import create_client, Client
import io

st.set_page_config(page_title="Marketing & Sales Dashboard", layout="wide")
st.title("ActiveCampaign + WooCommerce ROI Dashboard")

# --- 1. LOAD AND PREP THE DATA VIA SUPABASE ---
@st.cache_data
def load_data():
    # Connect to Supabase using Streamlit Secrets
    url: str = st.secrets["supabase"]["url"]
    key: str = st.secrets["supabase"]["key"]
    supabase: Client = create_client(url, key)

    # Download ActiveCampaign Data securely
    ac_bytes = supabase.storage.from_('dashboard-data').download('ac_contacts.csv.gz')
    ac_df = pd.read_csv(io.BytesIO(ac_bytes), compression='gzip') 
    ac_df = ac_df[['Email', 'Date Created']].dropna(subset=['Email'])
    ac_df['Email'] = ac_df['Email'].str.lower().str.strip()
    ac_df['Date Created'] = pd.to_datetime(ac_df['Date Created'], errors='coerce')

    # Download WooCommerce Data securely
    woo_bytes = supabase.storage.from_('dashboard-data').download('woo_orders.tsv.gz')
    woo_df = pd.read_csv(io.BytesIO(woo_bytes), compression='gzip', sep="\t")
    
    woo_df = woo_df.rename(columns={
        'billing_email': 'Email', 
        'date_created_gmt': 'Order Date', 
        'total_amount': 'Order Total',
        'order_type': 'Order Type',
        'subscription_ids': 'Subscription IDs'
    })
    if 'Order Type' not in woo_df.columns:
        woo_df['Order Type'] = 'one_time'
    if 'Subscription IDs' not in woo_df.columns:
        woo_df['Subscription IDs'] = ''
    woo_df = woo_df[['Email', 'Order Date', 'Order Total', 'Order Type', 'Subscription IDs']].dropna(subset=['Email'])
    woo_df['Email'] = woo_df['Email'].str.lower().str.strip()
    woo_df['Order Date'] = pd.to_datetime(woo_df['Order Date'], errors='coerce')
    woo_df['Order Total'] = pd.to_numeric(woo_df['Order Total'], errors='coerce').fillna(0)
    woo_df['Order Type'] = woo_df['Order Type'].fillna('one_time')
    woo_df['Subscription IDs'] = woo_df['Subscription IDs'].fillna('')

    # Calculate overlapping start date 
    ac_min_date = ac_df['Date Created'].min()
    woo_min_date = woo_df['Order Date'].min()
    valid_start_date = max(ac_min_date, woo_min_date)

    # Merge the datasets
    merged = pd.merge(woo_df, ac_df, on='Email', how='left')
    merged = merged[merged['Order Date'] >= merged['Date Created']]
    
    # Calculate Sequential Order Data
    merged = merged.sort_values(['Email', 'Order Date'])
    merged['Order Sequence'] = merged.groupby('Email').cumcount() + 1
    non_renewal_mask = merged['Order Type'] != 'subscription_renewal'
    merged['Purchase Sequence'] = float('nan')
    merged.loc[non_renewal_mask, 'Purchase Sequence'] = (
        merged[non_renewal_mask].groupby('Email').cumcount() + 1
    )
    merged['Days Since Subscription'] = (merged['Order Date'] - merged['Date Created']).dt.days
    merged['Days Since Previous Order'] = float('nan')
    merged.loc[non_renewal_mask, 'Days Since Previous Order'] = (
        merged[non_renewal_mask].groupby('Email')['Order Date'].diff().dt.days
    )

    return merged, valid_start_date

# Load the data into the app
with st.spinner("Securely fetching data from Supabase and crunching 260,000+ orders..."):
    try:
        df, valid_start_date = load_data()
    except Exception as e:
        st.error(f"Failed to load data. Did you add the Supabase secrets? Error: {e}")
        st.stop()

# --- 2. SIDEBAR DATE FILTERS ---
st.sidebar.header("Filter by Order Date")

min_allowed_date = valid_start_date.date()
max_date = df['Order Date'].max().date()

st.sidebar.info(f"Historical data restricted to start on **{min_allowed_date}** to ensure alignment between WooCommerce and ActiveCampaign.")

start_date = st.sidebar.date_input("Start Date", min_allowed_date, min_value=min_allowed_date, max_value=max_date)
end_date = st.sidebar.date_input("End Date", max_date, min_value=min_allowed_date, max_value=max_date)

filtered_df = df[(df['Order Date'].dt.date >= start_date) & (df['Order Date'].dt.date <= end_date)]


def format_money(value):
    return f"${value:,.2f}" if not pd.isna(value) else "N/A"


def format_pct(value):
    return f"{value:.1%}" if not pd.isna(value) else "N/A"


def build_customer_model(source_df):
    first_orders = source_df[source_df['Purchase Sequence'] == 1][['Email', 'Order Date']]
    first_orders = first_orders.rename(columns={'Order Date': 'First Order Date'})

    customers = source_df.groupby('Email').agg(
        Total_Revenue=('Order Total', 'sum'),
        Total_Orders=('Order Total', 'size'),
        Last_Order_Date=('Order Date', 'max')
    ).reset_index()
    customers = customers.merge(first_orders, on='Email', how='left')

    repeat_window = source_df.merge(first_orders, on='Email', how='left')
    repeat_window['Days After First Order'] = (
        repeat_window['Order Date'] - repeat_window['First Order Date']
    ).dt.days
    repeated_90 = repeat_window[
        (repeat_window['Purchase Sequence'] > 1)
        & (repeat_window['Days After First Order'] <= 90)
    ]['Email'].unique()

    customers['Repeated Within 90 Days'] = customers['Email'].isin(repeated_90)
    return customers

# --- 3. CALCULATE METRICS ---
if not filtered_df.empty:
    st.subheader(f"Metrics for {start_date} to {end_date}")

    new_purchase_df = filtered_df[filtered_df['Order Type'] != 'subscription_renewal'].copy()
    renewal_df = filtered_df[filtered_df['Order Type'] == 'subscription_renewal'].copy()
    
    total_revenue = filtered_df['Order Total'].sum()
    unique_buyers = filtered_df['Email'].nunique()
    total_orders = len(filtered_df)
    average_order_value = total_revenue / total_orders if total_orders > 0 else 0
    new_purchase_revenue = new_purchase_df['Order Total'].sum()
    new_purchase_orders = len(new_purchase_df)
    new_purchase_aov = new_purchase_revenue / new_purchase_orders if new_purchase_orders > 0 else 0
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Revenue", f"${total_revenue:,.2f}")
    col2.metric("Unique Buyers", f"{unique_buyers:,}")
    col3.metric("Average Order Value", format_money(average_order_value))
    col4.metric("New Purchase AOV", format_money(new_purchase_aov))

    st.divider()

    # --- 4. FINANCIAL AND TRACKING MODELS ---
    st.subheader("Financial & Retention Models")

    buyer_orders = new_purchase_df.groupby('Email')['Order Total'].agg(['sum', 'count'])
    repeat_buyers = (buyer_orders['count'] > 1).sum()
    new_purchase_buyers = new_purchase_df['Email'].nunique()
    repeat_buyer_rate = repeat_buyers / new_purchase_buyers if new_purchase_buyers > 0 else 0
    revenue_per_buyer = total_revenue / unique_buyers if unique_buyers > 0 else 0
    returning_revenue = new_purchase_df[new_purchase_df['Purchase Sequence'] > 1]['Order Total'].sum()
    returning_revenue_share = returning_revenue / new_purchase_revenue if new_purchase_revenue > 0 else 0

    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    f_col1.metric("Observed Revenue per Buyer", format_money(revenue_per_buyer))
    f_col2.metric("Repeat Buyer Rate", format_pct(repeat_buyer_rate))
    f_col3.metric("Non-Renewal Returning Revenue", format_pct(returning_revenue_share))
    f_col4.metric("Repeat Buyers", f"{repeat_buyers:,}")

    order_type_summary = filtered_df.groupby('Order Type').agg(
        Orders=('Order Total', 'size'),
        Revenue=('Order Total', 'sum'),
        Buyers=('Email', 'nunique')
    ).reset_index()
    order_type_summary['Revenue Share'] = order_type_summary['Revenue'] / total_revenue if total_revenue > 0 else 0
    order_type_summary['Avg Transaction Value'] = order_type_summary['Revenue'] / order_type_summary['Orders']
    order_type_summary = order_type_summary.sort_values('Revenue', ascending=False)
    display_order_types = order_type_summary.copy()
    display_order_types['Revenue'] = display_order_types['Revenue'].map(format_money)
    display_order_types['Revenue Share'] = display_order_types['Revenue Share'].map(format_pct)
    display_order_types['Avg Transaction Value'] = display_order_types['Avg Transaction Value'].map(format_money)

    renewal_revenue = renewal_df['Order Total'].sum()
    renewal_orders = len(renewal_df)
    renewal_avg = renewal_revenue / renewal_orders if renewal_orders > 0 else 0
    renewal_share = renewal_revenue / total_revenue if total_revenue > 0 else 0

    s_col1, s_col2, s_col3, s_col4 = st.columns(4)
    s_col1.metric("Renewal Revenue", format_money(renewal_revenue))
    s_col2.metric("Renewal Revenue Share", format_pct(renewal_share))
    s_col3.metric("Renewal Orders", f"{renewal_orders:,}")
    s_col4.metric("Avg Renewal Amount", format_money(renewal_avg))

    model_rows = [
        {
            "Model": "Average Order Value",
            "Formula": "Revenue / All Transactions",
            "Current Value": format_money(average_order_value),
            "Use": "Blended transaction value, including subscription renewals"
        },
        {
            "Model": "New Purchase AOV",
            "Formula": "Non-Renewal Revenue / Non-Renewal Orders",
            "Current Value": format_money(new_purchase_aov),
            "Use": "Pricing, bundles, upsells, and offer quality excluding automatic renewals"
        },
        {
            "Model": "Observed Customer Value",
            "Formula": "Revenue / Buyers",
            "Current Value": format_money(revenue_per_buyer),
            "Use": "How much matched AC buyers are worth so far"
        },
        {
            "Model": "Repeat Purchase Rate",
            "Formula": "Buyers with 2+ non-renewal orders / Non-renewal buyers",
            "Current Value": format_pct(repeat_buyer_rate),
            "Use": "Retention strength excluding automatic subscription renewals"
        },
        {
            "Model": "Returning Revenue Share",
            "Formula": "Non-renewal revenue from purchase #2+ / Non-renewal revenue",
            "Current Value": format_pct(returning_revenue_share),
            "Use": "Repeat buying behavior without subscription renewal cadence"
        }
    ]
    st.dataframe(pd.DataFrame(model_rows), hide_index=True, width='stretch')

    st.markdown("##### Revenue by Order Type")
    st.dataframe(display_order_types, hide_index=True, width='stretch')

    customer_source = df[df['Order Type'] != 'subscription_renewal'].copy()
    customers = build_customer_model(customer_source)
    cohort_customers = customers[
        (customers['First Order Date'].dt.date >= start_date)
        & (customers['First Order Date'].dt.date <= end_date)
    ].copy()

    if not cohort_customers.empty:
        cohort_customers['First Order Month'] = cohort_customers['First Order Date'].dt.to_period('M').astype(str)
        cohort_summary = cohort_customers.groupby('First Order Month').agg(
            Customers=('Email', 'count'),
            Revenue=('Total_Revenue', 'sum'),
            Orders=('Total_Orders', 'sum'),
            Repeat_Buyers=('Total_Orders', lambda orders: (orders > 1).sum()),
            Repeat_Within_90_Days=('Repeated Within 90 Days', 'sum')
        ).reset_index()
        cohort_summary['Revenue per Buyer'] = cohort_summary['Revenue'] / cohort_summary['Customers']
        cohort_summary['Repeat Buyer Rate'] = cohort_summary['Repeat_Buyers'] / cohort_summary['Customers']
        cohort_summary['90-Day Repeat Rate'] = cohort_summary['Repeat_Within_90_Days'] / cohort_summary['Customers']

        display_cohorts = cohort_summary.tail(12).copy()
        display_cohorts['Revenue'] = display_cohorts['Revenue'].map(format_money)
        display_cohorts['Revenue per Buyer'] = display_cohorts['Revenue per Buyer'].map(format_money)
        display_cohorts['Repeat Buyer Rate'] = display_cohorts['Repeat Buyer Rate'].map(format_pct)
        display_cohorts['90-Day Repeat Rate'] = display_cohorts['90-Day Repeat Rate'].map(format_pct)
        display_cohorts = display_cohorts[[
            'First Order Month',
            'Customers',
            'Revenue',
            'Revenue per Buyer',
            'Repeat Buyer Rate',
            '90-Day Repeat Rate'
        ]]

        st.markdown("##### First-Order Cohort Tracking")
        st.dataframe(display_cohorts, hide_index=True, width='stretch')
    else:
        st.info("No first-order cohorts found in this selected date range.")

    st.caption("Revenue metrics include all matched WooCommerce transactions. Purchase behavior, repeat rate, and cohort metrics exclude subscription renewals so recurring billing does not inflate new-order behavior. CAC, ROAS, and channel attribution need spend/source fields that are not currently in the dashboard data.")

    st.divider()

    # --- 5. PURCHASE DURATION ANALYSIS ---
    st.subheader("⏱️ Purchase Velocity (Averages)")
    
    v_col1, v_col2 = st.columns(2)
    
    with v_col1:
        st.markdown("**Initial Conversion**")
        first_orders = new_purchase_df[new_purchase_df['Purchase Sequence'] == 1]
        avg_days_to_first = first_orders['Days Since Subscription'].mean()
        avg_first_val = first_orders['Order Total'].mean()
        
        st.metric("Avg Days: Lead ➔ 1st Order", f"{avg_days_to_first:.1f} Days" if not pd.isna(avg_days_to_first) else "N/A")
        st.metric("Avg Value: 1st Order", f"${avg_first_val:.2f}" if not pd.isna(avg_first_val) else "N/A")

    with v_col2:
        st.markdown("**Subsequent Purchases**")
        seq_data = []
        for seq in range(2, 6): 
            seq_orders = new_purchase_df[new_purchase_df['Purchase Sequence'] == seq]
            if not seq_orders.empty:
                avg_gap = seq_orders['Days Since Previous Order'].mean()
                avg_val = seq_orders['Order Total'].mean()
                seq_data.append({
                    "Purchase #": f"Order {seq-1} ➔ Order {seq}",
                    "Avg Days Between": round(avg_gap, 1),
                    "Avg Order Value": f"${avg_val:.2f}"
                })
        
        if seq_data:
            seq_df = pd.DataFrame(seq_data)
            st.dataframe(seq_df, hide_index=True, width='stretch')
        else:
            st.info("Not enough repeat purchase data in this timeframe.")

    if seq_data:
        st.markdown("##### Average Days Between Purchases")
        chart_df = pd.DataFrame(seq_data).set_index("Purchase #")[["Avg Days Between"]]
        st.bar_chart(chart_df)

else:
    st.warning("No orders found in this date range.")
