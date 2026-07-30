from .mysql import MariaDBAdapter, MySQLAdapter
from .postgres import PostgresAdapter
from .sqlite import SQLiteAdapter

ADAPTERS = {
    "postgres": PostgresAdapter(),
    "mysql": MySQLAdapter(),
    "mariadb": MariaDBAdapter(),
    "sqlite": SQLiteAdapter(),
}
