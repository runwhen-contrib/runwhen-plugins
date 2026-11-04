from azure_aks_health.common import read_settings
from azure_aks_health.node_pools import read


def main(ctx):
    st = read_settings(ctx.settings)
    return {"nodePools": read(ctx, st.max_items)}
