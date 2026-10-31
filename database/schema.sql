-- PDS Blind Weight Verification - MySQL 8 schema snapshot
-- Generated from SQLAlchemy models by database/export_schema.py. Do not edit by hand.
-- Apply with:  mysql -u pds -p pds < database/schema.sql   (or use `flask db upgrade`)

SET NAMES utf8mb4;

CREATE TABLE fps_locations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(16) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	district VARCHAR(80) NOT NULL, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

CREATE TABLE mls_locations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(16) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	district VARCHAR(80) NOT NULL, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

CREATE TABLE roles (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	name VARCHAR(30) NOT NULL, 
	description VARCHAR(200) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE users (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	username VARCHAR(40) NOT NULL, 
	full_name VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role_id INTEGER NOT NULL, 
	mls_location_id INTEGER, 
	fps_location_id INTEGER, 
	fabric_identity VARCHAR(64), 
	is_enabled BOOL NOT NULL, 
	failed_logins INTEGER NOT NULL, 
	locked_until DATETIME, 
	last_login_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	FOREIGN KEY(role_id) REFERENCES roles (id), 
	FOREIGN KEY(mls_location_id) REFERENCES mls_locations (id), 
	FOREIGN KEY(fps_location_id) REFERENCES fps_locations (id)
);
CREATE INDEX ix_users_fps_location_id ON users (fps_location_id);
CREATE INDEX ix_users_mls_location_id ON users (mls_location_id);
CREATE INDEX ix_users_role_id ON users (role_id);

CREATE TABLE audit_logs (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER, 
	username VARCHAR(40), 
	action VARCHAR(40) NOT NULL, 
	entity_type VARCHAR(30), 
	entity_id VARCHAR(64), 
	detail TEXT, 
	ip_address VARCHAR(45), 
	created_at DATETIME NOT NULL, 
	prev_hash VARCHAR(64) NOT NULL, 
	entry_hash VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);
CREATE INDEX ix_audit_logs_created_at ON audit_logs (created_at);
CREATE INDEX ix_audit_logs_entity_id ON audit_logs (entity_id);
CREATE INDEX ix_audit_logs_user_id ON audit_logs (user_id);

CREATE TABLE consignments (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(40) NOT NULL, 
	mls_id INTEGER NOT NULL, 
	fps_id INTEGER NOT NULL, 
	created_by INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code), 
	FOREIGN KEY(mls_id) REFERENCES mls_locations (id), 
	FOREIGN KEY(fps_id) REFERENCES fps_locations (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);
CREATE INDEX ix_consignments_fps_id ON consignments (fps_id);
CREATE INDEX ix_consignments_mls_id ON consignments (mls_id);

CREATE TABLE settings (
	`key` VARCHAR(60) NOT NULL, 
	value VARCHAR(200) NOT NULL, 
	updated_by INTEGER, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (`key`), 
	FOREIGN KEY(updated_by) REFERENCES users (id)
);

CREATE TABLE bags (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bag_id VARCHAR(16) NOT NULL, 
	commodity VARCHAR(50) NOT NULL, 
	consignment_id INTEGER NOT NULL, 
	mls_id INTEGER NOT NULL, 
	fps_id INTEGER NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	created_by INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	register_tx_id VARCHAR(80), 
	PRIMARY KEY (id), 
	UNIQUE (bag_id), 
	FOREIGN KEY(consignment_id) REFERENCES consignments (id), 
	FOREIGN KEY(mls_id) REFERENCES mls_locations (id), 
	FOREIGN KEY(fps_id) REFERENCES fps_locations (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);
CREATE INDEX ix_bags_consignment_id ON bags (consignment_id);
CREATE INDEX ix_bags_fps_status ON bags (fps_id, status);
CREATE INDEX ix_bags_mls_status ON bags (mls_id, status);

CREATE TABLE blockchain_transactions (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	tx_id VARCHAR(80), 
	bag_pk INTEGER, 
	`function` VARCHAR(40) NOT NULL, 
	fabric_identity VARCHAR(64) NOT NULL, 
	user_id INTEGER, 
	block_number BIGINT, 
	status VARCHAR(12) NOT NULL, 
	error TEXT, 
	request_id VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tx_id), 
	FOREIGN KEY(bag_pk) REFERENCES bags (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_blockchain_transactions_bag_pk ON blockchain_transactions (bag_pk);
CREATE INDEX ix_blockchain_transactions_created_at ON blockchain_transactions (created_at);
CREATE INDEX ix_blockchain_transactions_status ON blockchain_transactions (status);
CREATE INDEX ix_blockchain_transactions_user_id ON blockchain_transactions (user_id);

CREATE TABLE corrections (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bag_pk INTEGER NOT NULL, 
	note VARCHAR(500) NOT NULL, 
	admin_id INTEGER NOT NULL, 
	tx_id VARCHAR(80), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(bag_pk) REFERENCES bags (id), 
	FOREIGN KEY(admin_id) REFERENCES users (id)
);
CREATE INDEX ix_corrections_bag_pk ON corrections (bag_pk);

CREATE TABLE dispatch_records (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bag_pk INTEGER NOT NULL, 
	weight_kg NUMERIC(8, 3) NOT NULL, 
	salt VARCHAR(64) NOT NULL, 
	commitment VARCHAR(64) NOT NULL, 
	operator_id INTEGER NOT NULL, 
	mls_id INTEGER NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	tx_id VARCHAR(80), 
	dispatched_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (bag_pk), 
	FOREIGN KEY(bag_pk) REFERENCES bags (id), 
	FOREIGN KEY(operator_id) REFERENCES users (id), 
	FOREIGN KEY(mls_id) REFERENCES mls_locations (id), 
	UNIQUE (request_id)
);
CREATE INDEX ix_dispatch_records_mls_id ON dispatch_records (mls_id);
CREATE INDEX ix_dispatch_records_operator_id ON dispatch_records (operator_id);

CREATE TABLE receipt_records (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bag_pk INTEGER NOT NULL, 
	weight_kg NUMERIC(8, 3) NOT NULL, 
	salt VARCHAR(64) NOT NULL, 
	commitment VARCHAR(64) NOT NULL, 
	operator_id INTEGER NOT NULL, 
	fps_id INTEGER NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	tx_id VARCHAR(80), 
	received_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (bag_pk), 
	FOREIGN KEY(bag_pk) REFERENCES bags (id), 
	FOREIGN KEY(operator_id) REFERENCES users (id), 
	FOREIGN KEY(fps_id) REFERENCES fps_locations (id), 
	UNIQUE (request_id)
);
CREATE INDEX ix_receipt_records_fps_id ON receipt_records (fps_id);
CREATE INDEX ix_receipt_records_operator_id ON receipt_records (operator_id);

CREATE TABLE reconciliation_records (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	bag_pk INTEGER NOT NULL, 
	dispatch_weight_kg NUMERIC(8, 3) NOT NULL, 
	receipt_weight_kg NUMERIC(8, 3) NOT NULL, 
	difference_kg NUMERIC(8, 3) NOT NULL, 
	tolerance_kg NUMERIC(6, 3) NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	chain_status VARCHAR(12) NOT NULL, 
	chain_tx_id VARCHAR(80), 
	computed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (bag_pk), 
	FOREIGN KEY(bag_pk) REFERENCES bags (id)
);
CREATE INDEX ix_reconciliation_records_status ON reconciliation_records (status);

