from pg_base.permissions import check


def main(ctx, rules):
    return {"access": check(ctx, list(rules))}
