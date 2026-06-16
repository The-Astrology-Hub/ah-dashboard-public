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
        'total_amount': 'Order Total'
    })
    woo_df = woo_df[['Email', 'Order Date', 'Order Total']].dropna(subset=['Email'])
    woo_df['Email'] = woo_df['Email'].str.lower().str.strip()
    woo_df['Order Date'] = pd.to_datetime(woo_df['Order Date'], errors='coerce')
    woo_df['Order Total'] = pd.to_numeric(woo_df['Order Total'], errors='coerce').fillna(0)

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
    merged['Days Since Subscription'] = (merged['Order Date'] - merged['Date Created']).dt.days
    merged['Days Since Previous Order'] = merged.groupby('Email')['Order Date'].diff().dt.days

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
    first_orders = source_df[source_df['Order Sequence'] == 1][['Email', 'Order Date']]
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
        (repeat_window['Order Sequence'] > 1)
        & (repeat_window['Days After First Order'] <= 90)
    ]['Email'].unique()

    customers['Repeated Within 90 Days'] = customers['Email'].isin(repeated_90)
    return customers

# --- 3. CALCULATE METRICS ---
if not filtered_df.empty:
    st.subheader(f"Metrics for {start_date} to {end_date}")
    
    total_revenue = filtered_df['Order Total'].sum()
    unique_buyers = filtered_df['Email'].nunique()
    total_orders = len(filtered_df)
    average_order_value = total_revenue / total_orders if total_orders > 0 else 0
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Revenue", f"${total_revenue:,.2f}")
    col2.metric("Unique Buyers", f"{unique_buyers:,}")
    col3.metric("Average Order Value", format_money(average_order_value))
    col4.metric("Avg Orders per Buyer", f"{(total_orders / unique_buyers):.1f}" if unique_buyers > 0 else "0")

    st.divider()

    # --- 4. FINANCIAL AND TRACKING MODELS ---
    st.subheader("Financial & Retention Models")

    buyer_orders = filtered_df.groupby('Email')['Order Total'].agg(['sum', 'count'])
    repeat_buyers = (buyer_orders['count'] > 1).sum()
    repeat_buyer_rate = repeat_buyers / unique_buyers if unique_buyers > 0 else 0
    revenue_per_buyer = total_revenue / unique_buyers if unique_buyers > 0 else 0
    returning_revenue = filtered_df[filtered_df['Order Sequence'] > 1]['Order Total'].sum()
    returning_revenue_share = returning_revenue / total_revenue if total_revenue > 0 else 0

    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    f_col1.metric("Observed Revenue per Buyer", format_money(revenue_per_buyer))
    f_col2.metric("Repeat Buyer Rate", format_pct(repeat_buyer_rate))
    f_col3.metric("Returning Revenue Share", format_pct(returning_revenue_share))
    f_col4.metric("Repeat Buyers", f"{repeat_buyers:,}")

    model_rows = [
        {
            "Model": "Average Order Value",
            "Formula": "Revenue / Orders",
            "Current Value": format_money(average_order_value),
            "Use": "Pricing, bundles, upsells, and offer quality"
        },
        {
            "Model": "Observed Customer Value",
            "Formula": "Revenue / Buyers",
            "Current Value": format_money(revenue_per_buyer),
            "Use": "How much matched AC buyers are worth so far"
        },
        {
            "Model": "Repeat Purchase Rate",
            "Formula": "Buyers with 2+ orders / Buyers",
            "Current Value": format_pct(repeat_buyer_rate),
            "Use": "Retention strength and post-purchase follow-up"
        },
        {
            "Model": "Returning Revenue Share",
            "Formula": "Revenue from order #2+ / Revenue",
            "Current Value": format_pct(returning_revenue_share),
            "Use": "How much revenue comes from repeat behavior"
        }
    ]
    st.dataframe(pd.DataFrame(model_rows), hide_index=True, width='stretch')

    customers = build_customer_model(df)
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

    st.caption("These models use matched ActiveCampaign contacts and WooCommerce orders only. CAC, ROAS, and channel attribution need spend/source fields that are not currently in the dashboard data.")

    st.divider()

    # --- 5. PURCHASE DURATION ANALYSIS ---
    st.subheader("⏱️ Purchase Velocity (Averages)")
    
    v_col1, v_col2 = st.columns(2)
    
    with v_col1:
        st.markdown("**Initial Conversion**")
        first_orders = filtered_df[filtered_df['Order Sequence'] == 1]
        avg_days_to_first = first_orders['Days Since Subscription'].mean()
        avg_first_val = first_orders['Order Total'].mean()
        
        st.metric("Avg Days: Lead ➔ 1st Order", f"{avg_days_to_first:.1f} Days" if not pd.isna(avg_days_to_first) else "N/A")
        st.metric("Avg Value: 1st Order", f"${avg_first_val:.2f}" if not pd.isna(avg_first_val) else "N/A")

    with v_col2:
        st.markdown("**Subsequent Purchases**")
        seq_data = []
        for seq in range(2, 6): 
            seq_orders = filtered_df[filtered_df['Order Sequence'] == seq]
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
