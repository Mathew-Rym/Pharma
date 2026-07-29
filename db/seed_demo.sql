-- Demo seed for multi-tenant isolation demo.
--
-- Two pharmacies, each with staff, customers, products, and batches.
-- Column names match the ACTUAL schema in schema.sql, not the breakdown's guesses.
--
-- Run AFTER all migrations (schema.sql through schema_v10.sql).
-- Idempotent: uses ON CONFLICT DO NOTHING throughout.
--
-- IMPORTANT: Do not run this against a production database. These are demo fixtures.

-- ============================================================ Pharmacy A: Good Life
-- (Already exists as c1457e5e... — the pilot pharmacy)

-- Only insert if the pharmacy doesn't already have products.
-- The pilot pharmacy might already have real data.

-- ============================================================ Pharmacy B: New Lemuma
-- A second pharmacy to demonstrate tenant isolation.
insert into pharmacies (id, name, ppb_licence, mpesa_paybill, wa_number, timezone)
values (
    'b2f84a3c-1111-4444-bbbb-000000000002',
    'New Lemuma Pharmacy',
    'PPB-LEM-2024',
    '4166920',
    '254700000099',
    'Africa/Nairobi'
) on conflict (id) do nothing;


-- ============================================================ Staff
-- Pharmacy A (Good Life) — existing pharmacy c1457e5e...
-- Your own number is already in staff. Add demo staff if needed.
insert into staff (pharmacy_id, phone, name, role, ppb_reg_no, is_active)
values
    ('c1457e5e-9f62-468b-ab50-b41382e83610', '254720521291', 'John Mwangi', 'owner', null, true)
on conflict (phone) do nothing;

-- Pharmacy B (New Lemuma) — demo staff
insert into staff (pharmacy_id, phone, name, role, ppb_reg_no, is_active)
values
    ('b2f84a3c-1111-4444-bbbb-000000000002', '254700000003', 'Sarah Otieno', 'owner', null, true),
    ('b2f84a3c-1111-4444-bbbb-000000000002', '254700000004', 'David Kamau', 'pharmacist', 'PPB-67890', true)
on conflict (phone) do nothing;


-- ============================================================ Customers
-- Pharmacy A
insert into customers (pharmacy_id, phone, name, consent_given, consent_at, loyalty_points)
values
    ('c1457e5e-9f62-468b-ab50-b41382e83610', '254700000005', 'Alice Wanjiku', true, now(), 50),
    ('c1457e5e-9f62-468b-ab50-b41382e83610', '254700000006', 'Brian Ochieng', true, now(), 120)
on conflict (pharmacy_id, phone) do nothing;

-- Pharmacy B
insert into customers (pharmacy_id, phone, name, consent_given, consent_at, loyalty_points)
values
    ('b2f84a3c-1111-4444-bbbb-000000000002', '254700000007', 'Catherine Muthoni', true, now(), 80),
    ('b2f84a3c-1111-4444-bbbb-000000000002', '254700000008', 'Dennis Kipchoge', true, now(), 30)
on conflict (pharmacy_id, phone) do nothing;


-- ============================================================ Suppliers
-- Pharmacy A
insert into suppliers (pharmacy_id, code, name, phone, rep_name, lead_time_days)
values
    ('c1457e5e-9f62-468b-ab50-b41382e83610', 'SUP-GL-001', 'MedTrack Distributors', '254711000111', 'Vivian', 2),
    ('c1457e5e-9f62-468b-ab50-b41382e83610', 'SUP-GL-002', 'PharmAccess Kenya', '254711000222', 'James', 3)
on conflict do nothing;

-- Pharmacy B
insert into suppliers (pharmacy_id, code, name, phone, rep_name, lead_time_days)
values
    ('b2f84a3c-1111-4444-bbbb-000000000002', 'SUP-NL-001', 'Cosmos Pharma', '254711000333', 'Grace', 2)
on conflict do nothing;


-- ============================================================ Products + Batches
-- Use a DO block so we can capture product IDs for batch inserts.

do $$
declare
    pid_a uuid := 'c1457e5e-9f62-468b-ab50-b41382e83610';
    pid_b uuid := 'b2f84a3c-1111-4444-bbbb-000000000002';
    v_prod_id uuid;
begin
    -- ======================== PHARMACY A products ========================

    -- Amoxicillin 500mg Caps 30s
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_a, 'CAPS-AMX500', 'Amoxicillin 500mg Caps 30s', 'Amoxicillin',
            'caps', '500mg', 30, true, 60, 8.50, 12.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_a, v_prod_id, 'AMX-B001', '2027-12-31', 150);
    end if;

    -- Paracetamol 500mg Tabs 100s
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_a, 'TABS-PCM500', 'Paracetamol 500mg Tabs 100s', 'Paracetamol',
            'tabs', '500mg', 100, false, 200, 1.50, 3.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_a, v_prod_id, 'PCM-B001', '2028-06-30', 500);
    end if;

    -- Metformin 500mg Tabs 30s
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_a, 'TABS-MET500', 'Metformin 500mg Tabs 30s', 'Metformin',
            'tabs', '500mg', 30, true, 60, 4.00, 7.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_a, v_prod_id, 'MET-B001', '2027-09-30', 75);
    end if;

    -- Atorvastatin 20mg Tabs 30s (ONLY in Pharmacy A)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_a, 'TABS-ATV20', 'Atorvastatin 20mg Tabs 30s', 'Atorvastatin',
            'tabs', '20mg', 30, true, 30, 15.00, 22.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_a, v_prod_id, 'ATV-B001', '2028-03-31', 30);
    end if;

    -- Omeprazole 20mg Caps 14s (ONLY in Pharmacy A)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_a, 'CAPS-OMP20', 'Omeprazole 20mg Caps 14s', 'Omeprazole',
            'caps', '20mg', 14, false, 28, 6.00, 10.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_a, v_prod_id, 'OMP-B001', '2027-11-30', 100);
    end if;


    -- ======================== PHARMACY B products ========================

    -- Amoxicillin 250mg Syrup 100ml (ONLY in Pharmacy B — different strength)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_b, 'SYR-AMX250', 'Amoxicillin 250mg Syrup 100ml', 'Amoxicillin',
            'syrup', '250mg/5ml', 1, true, 10, 180.00, 250.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_b, v_prod_id, 'AMX-NL01', '2027-12-31', 25);
    end if;

    -- Paracetamol 500mg Tabs 100s (BOTH pharmacies — shows independent stock)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_b, 'TABS-PCM500', 'Paracetamol 500mg Tabs 100s', 'Paracetamol',
            'tabs', '500mg', 100, false, 200, 1.50, 2.50)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_b, v_prod_id, 'PCM-NL01', '2028-06-30', 300);
    end if;

    -- Cetirizine 10mg Tabs 30s (ONLY in Pharmacy B)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_b, 'TABS-CTZ10', 'Cetirizine 10mg Tabs 30s', 'Cetirizine',
            'tabs', '10mg', 30, false, 30, 3.00, 5.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_b, v_prod_id, 'CTZ-NL01', '2028-04-30', 80);
    end if;

    -- Metformin 500mg Tabs 30s (BOTH pharmacies — shows different stock levels)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_b, 'TABS-MET500', 'Metformin 500mg Tabs 30s', 'Metformin',
            'tabs', '500mg', 30, true, 60, 4.00, 6.50)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_b, v_prod_id, 'MET-NL01', '2027-09-30', 120);
    end if;

    -- Ibuprofen 400mg Tabs 30s (ONLY in Pharmacy B)
    insert into products (pharmacy_id, legacy_code, name, generic_name, form, strength,
                          pack_size, is_prescription_only, reorder_level_pieces,
                          cost_price, sell_price)
    values (pid_b, 'TABS-IBU400', 'Ibuprofen 400mg Tabs 30s', 'Ibuprofen',
            'tabs', '400mg', 30, false, 30, 2.50, 5.00)
    on conflict (pharmacy_id, legacy_code) do nothing
    returning id into v_prod_id;
    if v_prod_id is not null then
        insert into batches (pharmacy_id, product_id, batch_no, expiry_date, qty_pieces)
        values (pid_b, v_prod_id, 'IBU-NL01', '2028-01-31', 45);
    end if;

end $$;


-- ============================================================ Inbound history (Gate 3)
-- Seed inbound_history for all demo staff so they can receive messages immediately.
-- In production, this would come from actual WhatsApp conversations.
insert into inbound_history (pharmacy_id, phone)
select pharmacy_id, phone from staff where is_active
on conflict (pharmacy_id, phone) do nothing;

-- Seed for demo customers too
insert into inbound_history (pharmacy_id, phone)
select pharmacy_id, phone from customers where phone is not null and phone <> ''
on conflict (pharmacy_id, phone) do nothing;
