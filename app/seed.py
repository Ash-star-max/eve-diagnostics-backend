"""Idempotent seed data: a small catalogue, three centres and an optional admin.

Run with:  python -m app.seed
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.logging_config import configure_logging
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest, User, UserRole
from app.security import hash_password

logger = logging.getLogger("app.seed")

TESTS = [
    ("CBC", "Complete Blood Count", "Haemoglobin, RBC, WBC and platelet counts."),
    ("LFT", "Liver Function Test", "Enzymes and proteins that indicate liver health."),
    ("KFT", "Kidney Function Test", "Urea, creatinine and electrolytes."),
    ("TSH", "Thyroid Stimulating Hormone", "Screens for thyroid disorders."),
    ("HBA1C", "HbA1c", "Average blood sugar over roughly three months."),
    ("LIPID", "Lipid Profile", "Cholesterol and triglycerides."),
]

CENTRES = [
    ("CarePoint Diagnostics", "Plot 12, Sector 18", "Noida", "201301",
     {"CBC": "349.00", "LFT": "699.00", "TSH": "399.00", "LIPID": "599.00", "HBA1C": "499.00"}),
    ("Lifeline Labs", "A-24, Saket", "New Delhi", "110017",
     {"CBC": "299.00", "KFT": "649.00", "TSH": "449.00", "LIPID": "549.00"}),
    ("Metro Path Centre", "Tower B, Cyber City", "Gurugram", "122002",
     {"CBC": "399.00", "LFT": "749.00", "KFT": "699.00", "HBA1C": "549.00"}),
]


def seed(db: Session) -> None:
    tests: dict[str, DiagnosticTest] = {}
    for code, name, description in TESTS:
        test = db.scalar(select(DiagnosticTest).where(DiagnosticTest.code == code))
        if test is None:
            test = DiagnosticTest(code=code, name=name, description=description)
            db.add(test)
        tests[code] = test
    db.flush()

    for name, address, city, pincode, prices in CENTRES:
        centre = db.scalar(
            select(DiagnosticCentre).where(DiagnosticCentre.name == name, DiagnosticCentre.city == city)
        )
        if centre is None:
            centre = DiagnosticCentre(name=name, address=address, city=city, pincode=pincode)
            db.add(centre)
            db.flush()
        for code, price in prices.items():
            exists = db.scalar(
                select(CentreTest.id).where(CentreTest.centre_id == centre.id, CentreTest.test_id == tests[code].id)
            )
            if exists is None:
                db.add(CentreTest(centre_id=centre.id, test_id=tests[code].id, price=Decimal(price)))

    settings = get_settings()
    if settings.admin_email and settings.admin_password:
        email = settings.admin_email.lower()
        if db.scalar(select(User.id).where(User.email == email)) is None:
            db.add(User(email=email, full_name="Admin", password_hash=hash_password(settings.admin_password),
                        role=UserRole.ADMIN))
            logger.info("admin_created", extra={"email": email})
    db.commit()


def main() -> None:
    configure_logging(get_settings().log_level)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed(db)
    logger.info("seed_complete")


if __name__ == "__main__":
    main()
