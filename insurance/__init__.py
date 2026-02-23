import pymysql

# This line tells Django to use pymysql instead of the old tool
pymysql.version_info = (2, 2, 1, "final", 0)
pymysql.install_as_MySQLdb()