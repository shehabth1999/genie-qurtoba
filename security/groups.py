# -*- coding: utf-8 -*-
"""
Security groups for qurtoba module

This file defines all security groups for the qurtoba module.
Groups are synced to the database using the sync_groups management command.
"""

GROUPS = [
    {
        'name': 'Qurtoba Users',
        'technical_name': 'qurtoba.users',
        'category': 'Qurtoba',
        'description': 'Access qurtoba module',
    },
    {
        'name': 'Qurtoba Admins',
        'technical_name': 'qurtoba.admins',
        'category': 'Qurtoba',
        'implied_groups': ['qurtoba.users'],
        'description': 'Manage all qurtoba module',
    }
]
