-- MySQL / MariaDB schema for goippy

CREATE TABLE IF NOT EXISTS goippy_gateways (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ext VARCHAR(32) UNIQUE,
    goip_id VARCHAR(64),
    goip_pass VARCHAR(64),
    channel INT,
    msisdn VARCHAR(32),
    allow_regex VARCHAR(255),
    enabled TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS goippy_calls (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts INT,
    goip_id VARCHAR(64),
    ext VARCHAR(32),
    direction TINYINT,
    remote_num VARCHAR(32),
    cause VARCHAR(32),
    msisdn VARCHAR(32),
    raw VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX(goip_id),
    INDEX(ext),
    INDEX(remote_num)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS goippy_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    dir ENUM('in','out'),
    msisdn VARCHAR(32),
    xmpp_jid VARCHAR(255),
    body TEXT,
    status VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
