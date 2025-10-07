# -*- coding: utf-8 -*-
from .plugin import QgisMonitorProPlugin

def classFactory(iface):
    return QgisMonitorProPlugin(iface)
