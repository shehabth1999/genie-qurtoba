# -*- coding: utf-8 -*-
menu_dict = {
    "qurtoba_main_menu": {
        "name": "قرطبة",
        "icon": "Database",
        "module": "qurtoba",
        "sequence": 90,
        "children": {

            # ================================================================
            # Level 2 — Flat: العملاء  (single item — no group wrapper)
            # ================================================================
            "qurtoba_menu_customers": {
                "name": "العملاء",
                "icon": "Users",
                "module": "qurtoba",
                "model": "qurtoba.qurtobacustomer",
                "view_types": "list,form",
                "sequence": 1,
            },

            # ================================================================
            # Level 2 — Group: المعاملات
            # ================================================================
            "qurtoba_group_transactions": {
                "name": "المعاملات",
                "icon": "FileText",
                "module": "qurtoba",
                "sequence": 2,
                "children": {
                    "qurtoba_menu_debt": {
                        "name": "تسجيل مديونية",
                        "icon": "FilePlus",
                        "module": "qurtoba",
                        "model": "qurtoba.qurtobarecord",
                        "view_types": "list,form",
                        "sequence": 1,
                        "domain": {
                            "filters": {
                                "operator": "and",
                                "filters": [
                                    {"field": "is_down", "operator": "eq", "value": False},
                                ]
                            }
                        },
                        "context": {
                            "default_fields": {
                                "is_down": False,
                                "is_seller": False,
                            }
                        },
                    },
                    "qurtoba_menu_collections": {
                        "name": "التحصيلات",
                        "icon": "CheckCircle",
                        "module": "qurtoba",
                        "model": "qurtoba.qurtobarecord",
                        "view_types": "list,form",
                        "sequence": 2,
                        "domain": {
                            "filters": {
                                "operator": "and",
                                "filters": [
                                    {"field": "is_down",   "operator": "eq", "value": True},
                                    {"field": "is_seller", "operator": "eq", "value": True},
                                ]
                            }
                        },
                        "context": {
                            "default_fields": {
                                "is_down": True,
                                "is_seller": True,
                                "type": "تحصيل",
                            }
                        },
                    },
                    "qurtoba_menu_records": {
                        "name": "جميع المعاملات",
                        "icon": "List",
                        "module": "qurtoba",
                        "model": "qurtoba.qurtobarecord",
                        "view_types": "list,form",
                        "sequence": 3,
                    },
                    "qurtoba_menu_account_tasks": {
                        "name": "فورى / أمان",
                        "icon": "ListTodo",
                        "module": "qurtoba",
                        "url": "/qurtoba/account-tasks/",
                        "sequence": 4,
                    },
                }
            },

            # ================================================================
            # Level 2 — Group: تطلب تقييم (Review queues)
            # ================================================================
            "qurtoba_group_pending": {
                "name": "تطلب تقييم",
                "icon": "AlertCircle",
                "module": "qurtoba",
                "sequence": 2.5,
                "children": {
                    "qurtoba_menu_pending_transactions": {
                        "name": "تحويلات تطلب تقييم",
                        "icon": "Clock",
                        "module": "qurtoba",
                        "model": "qurtoba.qurtobapendingtransaction",
                        "view_types": "list,form",
                        "sequence": 1,
                        "domain": {
                            "filters": {
                                "operator": "and",
                                "filters": [
                                    {"field": "review_state", "operator": "eq", "value": "pending"},
                                ]
                            }
                        },
                    },
                    "qurtoba_menu_pending_payments": {
                        "name": "سدادات تطلب تقييم",
                        "icon": "FileText",
                        "module": "qurtoba",
                        "model": "qurtoba.qurtobapendingpayment",
                        "view_types": "list,form",
                        "sequence": 2,
                        "domain": {
                            "filters": {
                                "operator": "and",
                                "filters": [
                                    {"field": "review_state", "operator": "eq", "value": "pending"},
                                ]
                            }
                        },
                    },
                }
            },

            # ================================================================
            # Level 2 — Group: التقارير
            # ================================================================
            "qurtoba_group_reports": {
                "name": "التقارير",
                "icon": "BarChart2",
                "module": "qurtoba",
                "sequence": 3,
                "children": {
                    "qurtoba_menu_customer_dues": {
                        "name": "مستحقات العملاء",
                        "icon": "Users",
                        "module": "qurtoba",
                        "url": "/qurtoba/customer-dues/",
                        "sequence": 1,
                    },
                    "qurtoba_menu_seller_dues": {
                        "name": "مستحقات المناديب",
                        "icon": "Truck",
                        "module": "qurtoba",
                        "url": "/qurtoba/seller-dues/",
                        "sequence": 2,
                    },
                    "qurtoba_menu_accountant_report": {
                        "name": "تقارير المحاسب",
                        "icon": "FileSpreadsheet",
                        "module": "qurtoba",
                        "url": "/qurtoba/accountant-report/",
                        "sequence": 3,
                    },
                    "qurtoba_menu_delayed": {
                        "name": "المتأخرات",
                        "icon": "AlertTriangle",
                        "module": "qurtoba",
                        "url": "/qurtoba/delayed/",
                        "sequence": 4,
                    },
                }
            },

            # ================================================================
            # Action-only — hidden from sidebar, used by @action slideovers
            # ================================================================
            "qurtoba_action_quick_debt": {
                "name": "تسجيل مديونية سريع",
                "icon": "FilePlus",
                "module": "qurtoba",
                "model": "qurtoba.qurtobarecord",
                "view_types": "form",
                "is_visible": False,
                "sequence": 99,
            },
            "qurtoba_action_quick_collection": {
                "name": "تسجيل تحصيل سريع",
                "icon": "CheckCircle",
                "module": "qurtoba",
                "model": "qurtoba.qurtobarecord",
                "view_types": "form",
                "is_visible": False,
                "sequence": 99,
            },
            # Quick transaction form (with account selector) — for WhatsApp / partner actions
            "qurtoba_action_quick_transaction": {
                "name": "معاملة جديدة",
                "icon": "CreditCard",
                "module": "qurtoba",
                "model": "qurtoba.qurtobarecord",
                "view_types": "form",
                "is_visible": False,
                "sequence": 99,
            },

        }
    }
}
