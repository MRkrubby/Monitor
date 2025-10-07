# -*- coding: utf-8 -*-
def classFactory(iface):
    from .plugin import QgisMonitorProPlugin
    return QgisMonitorProPlugin(iface)
