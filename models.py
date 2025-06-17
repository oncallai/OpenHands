from sqlalchemy import JSON, Column, Enum, String
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Feedback(Base):  # type: ignore
    __tablename__ = 'feedback'

    id = Column(String, primary_key=True)
    version = Column(String, nullable=False)
    email = Column(String, nullable=False)
    polarity = Column(
        Enum('positive', 'negative', name='polarity_enum'), nullable=False
    )
    permissions = Column(
        Enum('public', 'private', name='permissions_enum'), nullable=False
    )
    trajectory = Column(JSON, nullable=True)
