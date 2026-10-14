"""补齐关系同会话数据库约束；不重写已经应用的 V3 迁移。"""

from alembic import op

revision = "c9e40b17fa32"
down_revision = "b3f1c7d2e904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_leads_id_session", "leads", ["id", "session_id"], schema="app")
    op.create_unique_constraint(
        "uq_findings_id_session", "findings", ["id", "session_id"], schema="app"
    )
    for table, column in (("leads", "lead_id"), ("findings", "finding_id")):
        op.create_foreign_key(
            f"fk_links_{'lead' if table == 'leads' else 'finding'}_session",
            "lead_weakness_links",
            table,
            [column, "session_id"],
            ["id", "session_id"],
            source_schema="app",
            referent_schema="app",
            ondelete="RESTRICT",
        )
    op.create_check_constraint(
        op.f("ck_lead_weakness_links_link_role_valid"),
        "lead_weakness_links",
        "role IN ('basis', 'result')",
        schema="app",
    )
    op.create_check_constraint(
        op.f("ck_lead_weakness_links_link_state_valid"),
        "lead_weakness_links",
        "relation_state IN ('proposed', 'evidence_linked', 'refuted')",
        schema="app",
    )


def downgrade() -> None:
    for name in (
        "ck_lead_weakness_links_link_state_valid",
        "ck_lead_weakness_links_link_role_valid",
    ):
        op.drop_constraint(name, "lead_weakness_links", schema="app", type_="check")
    for name in ("fk_links_lead_session", "fk_links_finding_session"):
        op.drop_constraint(name, "lead_weakness_links", schema="app", type_="foreignkey")
    op.drop_constraint("uq_findings_id_session", "findings", schema="app", type_="unique")
    op.drop_constraint("uq_leads_id_session", "leads", schema="app", type_="unique")
