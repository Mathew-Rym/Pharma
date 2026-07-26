-- ============================================================
-- DISHII schema v3 — additive. Safe on top of v1 + v2.
--
-- PHYSICAL DELIVERY VERIFICATION for Loop A.
--
-- The gap: Loop A trusted the invoice for quantity. Pharmacies do not lose money
-- because invoices are misread — they lose it because what arrives does not match
-- what was invoiced, and nobody counted.
--
-- What this DOES NOT add, on purpose:
--
-- Four separate design reviews proposed new tables for this (Delivery,
-- DeliveryImages, VisionDetection, VisionVerification, ReceivingSession) plus
-- columns named invoice_qty / actual_received_qty / staff_confirmed_count. None of
-- that is needed, because v1 already had it:
--
--     grn_lines.qty_invoiced_pieces   -- what the invoice claims
--     grn_lines.qty_counted_pieces    -- "what staff physically counted"
--     grn_lines.flags                 -- already includes 'short_delivery'
--     grns.discrepancy_note           -- populated by approve()
--     grns                            -- IS the receiving session; it already holds
--                                        images, raw_extract, approved_by, approved_at
--
-- and approve() already prefers counted over invoiced when writing the ledger, and
-- already records the difference. Staff could already correct a count by replying
-- '5:2W'. Adding parallel columns would have created two sources of truth for the
-- same number, which is how the ledger starts disagreeing with itself.
--
-- So the ONLY real gaps were: nobody was ASKED to count, and nothing pre-filled the
-- count. This file adds just enough to hold the machine's opinion separately from
-- the human's confirmed number — never overwriting it.
-- ============================================================

-- ---------- photos of the goods, not just the paperwork ----------
-- Kept on grns rather than a new table: it is the same receiving event, and a
-- supplier dispute three weeks later is answered by opening one GRN and seeing the
-- invoice image, the delivery photos, the machine count, and who approved.
alter table grns add column if not exists goods_images jsonb default '[]';

-- 'awaiting_count' sits between extraction and review. Without it a restart mid-flow
-- would leave a GRN that looks ready to approve but was never counted.
alter table grns drop constraint if exists grns_status_check;
alter table grns add constraint grns_status_check
  check (status in ('parsing','awaiting_count','needs_review','approved','rejected'));

-- ---------- what the machine saw ----------
-- Deliberately SEPARATE from qty_counted_pieces. That column means "a human stands
-- behind this number"; these mean "a model guessed". Collapsing them would let an
-- unreviewed guess become ledger truth, and the ledger is the one thing in this
-- system that must never be a guess.
--
-- PACKS, not pieces. Vision cannot see 100 tablets inside a sealed carton — it counts
-- cartons. Pieces are derived later with pack_size, in the one place that conversion
-- already lives. Storing a piece count here would repeat the 30x understatement bug
-- that '2W0P' read as 2 pieces already caused once.
alter table grn_lines add column if not exists vision_packs int;
alter table grn_lines add column if not exists vision_loose int;
alter table grn_lines add column if not exists vision_confidence numeric(4,3);
alter table grn_lines add column if not exists vision_note text;   -- 'partially obscured'

-- ---------- a view the dashboard and WhatsApp both read ----------
-- Three-way comparison: invoice vs machine vs human.
create or replace view v_grn_verification as
select l.grn_id,
       l.line_no,
       coalesce(p.name, l.raw_description) as name,
       coalesce(p.pack_size, 1)            as pack_size,
       l.qty_invoiced_pieces,
       l.vision_packs,
       l.vision_loose,
       -- the machine's opinion converted to pieces, for comparison only
       case when l.vision_packs is not null
            then l.vision_packs * coalesce(p.pack_size, 1)
                 + coalesce(l.vision_loose, 0)
       end                                  as vision_pieces,
       l.vision_confidence,
       l.vision_note,
       l.qty_counted_pieces,
       -- what approve() will actually put in the ledger
       coalesce(l.qty_counted_pieces, l.qty_invoiced_pieces) as pieces_to_receive,
       case
         when l.vision_packs is null then null
         else (l.vision_packs * coalesce(p.pack_size, 1)
               + coalesce(l.vision_loose, 0)) - l.qty_invoiced_pieces
       end                                  as vision_variance
  from grn_lines l
  left join products p on p.id = l.product_id;
