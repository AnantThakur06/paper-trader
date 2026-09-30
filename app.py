"""Paper Trader. Run locally with:  streamlit run app.py"""
import streamlit as st

st.set_page_config(page_title="Paper Trader", page_icon="📈", layout="wide")

import ui  # noqa: E402  (must come after set_page_config)

ui.require_password()
broker = ui.get_broker()
ui.run_sync(broker)

ctx = ui.load_context(broker)
indices = ui.index_data(broker.prices, "indices")
st.session_state["ctx"] = ctx
st.session_state["indices"] = indices

st.logo("assets/logo.svg", size="large")
nav = st.navigation([
    st.Page("views/trade.py", title="Trade", icon=":material/show_chart:", default=True),
    st.Page("views/history.py", title="History", icon=":material/receipt_long:"),
    st.Page("views/settings.py", title="Settings", icon=":material/settings:"),
])
ui.sidebar(ctx)
ui.top_bar(ctx, indices)
ui.show_flash()
nav.run()
