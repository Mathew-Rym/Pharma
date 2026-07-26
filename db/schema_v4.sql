-- ============================================================
-- Pharma OS schema v4 — additive/rename. Safe on top of v1 + v2 + v3.
--
--   1. Rebrand: the product is Pharma OS. One column carried the old product name.
--   2. An unconfirmed vision discrepancy must not disappear at approval.
-- ============================================================

-- ---------- 1. rebrand the one column carrying the old product name ----------
-- The old name -> `ledger_pieces`, which is also plainly more descriptive: it is what
-- OUR ledger says, as opposed to `pos_pieces`, what the till says.
--
-- Guarded on the OLD column existing and the new one not, so this is idempotent and a
-- re-run after the rename is a no-op rather than an error. (Renaming a column to its
-- own name raises in Postgres.)
do $$
begin
  if exists (select 1 from information_schema.columns
              where table_name = 'stock_reconciliation'
                and column_name = 'dishii_pieces')
     and not exists (select 1 from information_schema.columns
                      where table_name = 'stock_reconciliation'
                        and column_name = 'ledger_pieces') then
    -- the view depends on the column, so it has to go first
    drop view if exists v_stock_variance;
    alter table stock_reconciliation rename column dishii_pieces to ledger_pieces;
  end if;
end $$;

create or replace view v_stock_variance as
select r.pharmacy_id, p.name, r.legacy_code, r.ledger_pieces, r.pos_pieces,
       r.variance, r.variance_value, r.status, r.ran_at
  from stock_reconciliation r
  left join products p on p.id = r.product_id
 where r.status = 'open' and r.variance <> 0
 order by abs(r.variance_value) desc nulls last;

-- ---------- 2. unresolved count discrepancies survive approval ----------
-- Previously: vision flags "invoice says 6 packs, I can see 3", the pharmacist does
-- not confirm either way and just replies OK. approve() then received the invoice
-- quantity and `discrepancy_note` only recorded lines where a HUMAN had entered a
-- different count — so the machine's warning vanished, and the ledger silently
-- recorded 6 packs as though nothing had been questioned.
--
-- That is the "knowingly incorrect before it enters the ledger" failure. Receiving
-- must still proceed (blocking is worse), but the disagreement has to remain visible
-- and answerable afterwards, not be quietly dropped.
alter table grns add column if not exists unresolved_count_note text;

comment on column grns.unresolved_count_note is
  'Lines where vision disagreed with the invoice and no human confirmed a count '
  'before approval. Receiving proceeded on invoice quantities; this is the audit '
  'trail of what was never answered.';

-- Surfaces them for the dashboard and any later supplier claim.
create or replace view v_open_receiving_discrepancies as
select g.id as grn_id,
       g.pharmacy_id,
       g.invoice_no,
       s.name as supplier,
       g.approved_at,
       st.name as approved_by,
       g.unresolved_count_note,
       g.discrepancy_note
  from grns g
  left join suppliers s on s.id = g.supplier_id
  left join staff st on st.id = g.approved_by
 where g.status = 'approved'
   and g.unresolved_count_note is not null
 order by g.approved_at desc;
