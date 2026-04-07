"""Real stdout captured from Garage CLI commands — used as test fixtures."""

STATUS_OUTPUT = """\
==== HEALTHY NODES ====
ID                Hostname    Address         Tags  Zone      Capacity  DataAvail        Version
7a58a5fa192ad6dd  garage-one  127.0.0.1:3901  []    canada-1  10.0 GB   16.3 GB (83.0%)  v2.2.0
"""

STATUS_OUTPUT_MULTI_NODE = """\
==== HEALTHY NODES ====
ID                Hostname    Address         Tags  Zone      Capacity  DataAvail        Version
7a58a5fa192ad6dd  garage-one  127.0.0.1:3901  []    canada-1  10.0 GB   16.3 GB (83.0%)  v2.2.0
b2c4d6e8f0a1b3c5  garage-two  192.168.1.2:3901  []  canada-2  20.0 GB   15.0 GB (75.0%)  v2.2.0
"""

STATUS_OUTPUT_EMPTY = """\
==== HEALTHY NODES ====
ID  Hostname  Address  Tags  Zone  Capacity  DataAvail  Version
"""

STATS_OUTPUT = """\
==== NODE [7a58a5fa192ad6dd] ====
Node ID:                7a58a5fa192ad6dd
Hostname:               garage-one
Garage version:         v2.2.0
Database engine:        sqlite
Disk usage:             available: 16.3 GiB, total: 19.6 GiB
Object count:           2
Block count:            3
"""

STATS_OUTPUT_EMPTY = """\
==== NODE [7a58a5fa192ad6dd] ====
Node ID:                7a58a5fa192ad6dd
Hostname:               garage-one
"""

BUCKET_LIST_OUTPUT = """\
ID                Created     Global aliases  Local aliases
f1dc32249aa1d80a  2026-04-07  obsidian-vault  \n\
"""

BUCKET_LIST_OUTPUT_MULTI = """\
ID                Created     Global aliases  Local aliases
f1dc32249aa1d80a  2026-04-07  obsidian-vault
a2bc34de56f78901  2026-04-07  backups
"""

BUCKET_LIST_OUTPUT_EMPTY = """\
ID  Created  Global aliases  Local aliases
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
