-- v10: markets now come from OpenStreetMap (supermarket, greengrocer, butcher...) and
-- prices from what the household notes down; kind is the OSM shop type, in Portuguese.
ALTER TABLE markets ADD COLUMN kind TEXT;
