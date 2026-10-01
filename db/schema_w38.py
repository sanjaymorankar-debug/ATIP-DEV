"""
Tables added in W38 (platform & compliance), applied by db/schema.py.

    compliance_run / compliance_result   SEC-05  each compliance-monitoring run and its check results
    regulatory_item                      ENT-14  the regulatory sign-off register (seeded by ops/regulatory.py)
"""

from ops.compliance import DDL as _CMP
from ops.regulatory import DDL as _REG

W38_TABLES = {
    "compliance_run": (_CMP[0], _CMP[2]),
    "compliance_result": (_CMP[1],),
    "regulatory_item": (_REG,),
}
