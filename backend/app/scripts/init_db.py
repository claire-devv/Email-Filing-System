from app.db.session import init_db
from app.services.rulebook_service import RulebookService


def main() -> None:
    init_db()
    RulebookService().reload_from_file()
    print("Database initialized and folder rulebook loaded.")


if __name__ == "__main__":
    main()
