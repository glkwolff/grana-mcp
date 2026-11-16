-- v14: where the household shops, which brands it buys and how much one unit of an item
-- weighs.
-- shop_places: a name pattern ("italo", "condor boa vista", "festval kennedy"); it matches
-- every store from the receipts whose name + neighbourhood has all its words, so one entry
-- covers a chain's branch however the receipt spells it. Price comparisons stay among them.
CREATE TABLE shop_places (
    key        TEXT PRIMARY KEY,               -- folded words, e.g. 'condor boa vista'
    label      TEXT NOT NULL,                  -- as the person said it
    created_at TEXT NOT NULL,
    created_by TEXT
);
-- item_brands: the brand bought for an item ('arroz' -> 'tio joao'); the price lookup
-- searches "arroz tio joao" and the list prefers that brand.
CREATE TABLE item_brands (
    search     TEXT PRIMARY KEY,
    brand      TEXT NOT NULL,                  -- folded words
    label      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
-- item_units: one unit of an item in kg and/or litres ('limao' -> 0.1 kg), set by the
-- assistant when a recipe counts units and stores sell by the kg (or the other way round).
CREATE TABLE item_units (
    search     TEXT PRIMARY KEY,
    kg_per_un  REAL,
    l_per_un   REAL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
