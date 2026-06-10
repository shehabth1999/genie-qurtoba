# qurtoba/security/model_permissions.py
"""
Access rights for qurtoba module.
Format: [view, add, change, delete] as [0/1, 0/1, 0/1, 0/1]
"""

MODEL_PERMISSIONS = [
    # Qurtobacustomeraccount
    {
        'model': 'qurtoba.qurtobacustomeraccount',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.qurtobacustomeraccount',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Qurtobacustomer
    {
        'model': 'qurtoba.qurtobacustomer',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.qurtobacustomer',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Qurtobarecord
    {
        'model': 'qurtoba.qurtobarecord',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.qurtobarecord',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Cashsysvippage
    {
        'model': 'qurtoba.cashsysvippage',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.cashsysvippage',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Cashsysplan
    {
        'model': 'qurtoba.cashsysplan',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.cashsysplan',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Qurtobapendingtransaction
    {
        'model': 'qurtoba.qurtobapendingtransaction',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.qurtobapendingtransaction',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },
    
    # Qurtobapendingpayment
    {
        'model': 'qurtoba.qurtobapendingpayment',
        'group': 'qurtoba.users',
        'permissions': [1, 1, 1, 0],  # view, add, change, no delete
    },
    {
        'model': 'qurtoba.qurtobapendingpayment',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },

    # Qurtobasyncproblem (failed-push dead-letter queue)
    # Rows are produced by the system; users can view + retry (change), not delete.
    {
        'model': 'qurtoba.qurtobasyncproblem',
        'group': 'qurtoba.users',
        'permissions': [1, 0, 1, 0],  # view, no add, change (retry), no delete
    },
    {
        'model': 'qurtoba.qurtobasyncproblem',
        'group': 'qurtoba.admins',
        'permissions': [1, 1, 1, 1],  # full access
    },

]

# Permission patterns for convenience
PERMISSION_PATTERNS = {
    'NONE': [0, 0, 0, 0],           # No access
    'VIEW_ONLY': [1, 0, 0, 0],      # View only
    'MANAGE': [1, 1, 1, 0],         # Manage but no delete
    'FULL': [1, 1, 1, 1],           # Full access
}

# Example using patterns:
# {
#     'model': 'app.model',
#     'group': 'app.users',
#     'permissions': PERMISSION_PATTERNS['MANAGE'],
# }
