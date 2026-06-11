"""Real stdout captured from Garage CLI commands - used as test fixtures."""

STATUS_OUTPUT = """\
==== HEALTHY NODES ====
ID                Hostname    Address         Tags  Zone      Capacity  DataAvail        Version
7a58a5fa192ad6dd  garage-one  127.0.0.1:3901  []    canada-1  10.0 GB   16.3 GB (83.0%)  v2.2.0
"""

STATUS_OUTPUT_MULTI_NODE = """\
2026-04-07T15:37:55.000000Z  INFO garage_net::netapp: Connected to 127.0.0.1:3901, negotiating handshake...
2026-04-07T15:37:55.000000Z  INFO garage_net::netapp: Connection established to 7a58a5fa192ad6dd
==== HEALTHY NODES ====
ID                Hostname    Address         Tags  Zone       Capacity  DataAvail        Version
7a58a5fa192ad6dd  garage-one  127.0.0.1:3901  []    canada-1   10.0 GB   16.3 GB (83.0%)  v2.2.0
ab12cd34ef56gh78  garage-two  10.0.0.2:3901   []    ca-east-2  20.0 GB   18.0 GB (90.0%)  v2.2.0
cd34ef56gh78ij90  garage-pi   10.0.0.3:3901   []    ca-home-1   5.0 GB    4.5 GB (90.0%)  v2.2.0
"""

STATUS_OUTPUT_EMPTY = """\
==== HEALTHY NODES ====
ID  Hostname  Address  Tags  Zone  Capacity  DataAvail  Version
"""

STATS_OUTPUT = """\
2026-04-07T20:55:20.265399Z  INFO garage_net::netapp: Connected to 127.0.0.1:3901, negotiating handshake...
2026-04-07T20:55:20.308272Z  INFO garage_net::netapp: Connection established to 7a58a5fa192ad6dd
==== NODE [7a58a5fa192ad6dd] ====
Node ID:                7a58a5fa192ad6dd
Hostname:               garage-one
Garage version:         v2.2.0
Garage features:        bundled-libs, consul-discovery, fjall, journald, k2v, kubernetes-discovery, lmdb, metrics, sqlite, syslog, telemetry-otlp
Rust compiler version:  1.91.0
Database engine:        sqlite3 v3.50.2 (using rusqlite crate)

Table stats:
  Table                  Items  MklItems  MklTodo  InsQueue  GcTodo
  admin_token            0      0         0        0         0
  bucket_v2              3      4         0        0         0
  bucket_alias           3      4         0        0         0
  key                    4      5         0        0         0
  object                 5      6         0        0         3
  bucket_object_counter  1      1         0        0         2
  multipart_upload       0      0         0        0         0
  bucket_mpu_counter     0      0         0        0         0
  version                7      7         0        0         6
  block_ref              1      1         0        0         0
  k2v_item               0      0         0        0         0
  k2v_index_counter_v2   0      0         0        0         0

Block manager stats:
  number of RC entries:       1 (~= number of blocks)
  resync queue length:        0
  blocks with resync errors:  0


==== CLUSTER STATISTICS ====
Storage nodes:
  ID                Hostname    Zone      Capacity  Part.  DataAvail                MetaAvail
  7a58a5fa192ad6dd  garage-one  canada-1  10.0 GB   256    16.3 GB/19.7 GB (83.0%)  16.3 GB/19.7 GB (83.0%)

Estimated available storage space cluster-wide (might be lower in practice):
  data: 16.3 GB
  metadata: 16.3 GB
"""

STATS_OUTPUT_EMPTY = """\
==== NODE [7a58a5fa192ad6dd] ====
Node ID:                7a58a5fa192ad6dd
Hostname:               garage-one
"""

BUCKET_INFO_OUTPUT = """\
==== BUCKET INFORMATION ====
Bucket:          f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:         2026-04-07 16:06:34.587 +00:00
Size:            5.7 kiB (5.8 KB)
Objects:         2
Website access:  false
Global alias:    obsidian-vault
==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  obsidian-key  \n\
"""

BUCKET_INFO_OUTPUT_NO_GLOBAL_ALIAS = """\
==== BUCKET INFORMATION ====
Bucket:          a9b8c7d6e5f4032110aabbccddeeff00112233445566778899aabbccddeeff00
Created:         2026-04-07 16:06:34.587 +00:00
Size:            5.7 kiB (5.8 KB)
Objects:         2
Website access:  false
==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  customer-bucket-display  \n\
"""

BUCKET_INFO_OUTPUT_NO_KEYS = """\
==== BUCKET INFORMATION ====
Bucket:          f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:         2026-04-07 16:06:34.587 +00:00
Size:            0 B (0 B)
Objects:         0
Website access:  false
Global alias:    empty-bucket
==== KEYS FOR THIS BUCKET ====
Permissions  Access key  Local aliases
"""

BUCKET_INFO_OUTPUT_WITH_QUOTAS = """\
==== BUCKET INFORMATION ====
Bucket:                       f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:                      2026-04-07 16:06:34.587 +00:00

Size:                         5.7 kiB (5.8 KB)
Objects:                      2

Website access:               false

Quotas:                       enabled
  maximum size:               953.7 MiB (1000.0 MB)
  maximum number of objects:  1000

Global alias:                 obsidian-vault

==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  obsidian-key  \n\
"""

BUCKET_INFO_OUTPUT_QUOTA_SIZE_ONLY = """\
==== BUCKET INFORMATION ====
Bucket:          f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:         2026-04-07 16:06:34.587 +00:00

Size:            5.7 kiB (5.8 KB)
Objects:         2

Website access:  false

Quotas:          enabled
  maximum size:  953.7 MiB (1000.0 MB)

Global alias:    obsidian-vault

==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  obsidian-key  \n\
"""

BUCKET_INFO_OUTPUT_WEBSITE_ENABLED = """\
==== BUCKET INFORMATION ====
Bucket:          f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:         2026-04-07 16:06:34.587 +00:00

Size:            5.7 kiB (5.8 KB)
Objects:         2

Website access:    true
  index document:  index.html
  error document:  (not defined)

Global alias:    obsidian-vault

==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  obsidian-key  \n\
"""

BUCKET_INFO_OUTPUT_WEBSITE_CUSTOM_ERROR = """\
==== BUCKET INFORMATION ====
Bucket:          f1dc32249aa1d80af4bf6e887443fefac616e56dfdacc29c4bf6fedf9ec20617
Created:         2026-04-07 16:06:34.587 +00:00

Size:            5.7 kiB (5.8 KB)
Objects:         2

Website access:    true
  index document:  index.html
  error document:  404.html

Global alias:    obsidian-vault

==== KEYS FOR THIS BUCKET ====
Permissions  Access key                                Local aliases
RWO          GK5e6fb0b4fa406ace8126a7db  obsidian-key  \n\
"""

KEY_LIST_OUTPUT = """\
ID                          Created     Name          Expiration
GK5e6fb0b4fa406ace8126a7db  2026-04-07  obsidian-key  never
"""

KEY_LIST_OUTPUT_MULTI = """\
ID                          Created     Name          Expiration
GK5e6fb0b4fa406ace8126a7db  2026-04-07  obsidian-key  never
GKa1b2c3d4e5f6a7b8c9d0e1f2  2026-04-07  backup-key    never
"""

KEY_LIST_OUTPUT_EMPTY = """\
ID  Created  Name  Expiration
"""

# The secret key in this fixture is REDACTED but preserves the real format.
# In production, this value must NEVER be logged at any level.
KEY_CREATE_OUTPUT = """\
Key name:     test-key
Key ID:       GKdeadbeef1234567890abcdef
Secret key:   REDACTED_SECRET_DO_NOT_LOG_abcdefghijklmnop
"""
