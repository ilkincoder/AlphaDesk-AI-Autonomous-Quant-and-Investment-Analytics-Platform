"""SQLAlchemy models for portfolios and their holdings."""

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Portfolio(Base):
    """A single portfolio of cash plus long positions.

    Money uses NUMERIC, never FLOAT: binary floating point cannot represent values
    like 0.10 exactly, so summing cash in floats drifts by tiny amounts.
    """

    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint("cash_balance >= 0", name="ck_portfolios_cash_balance_nonnegative"),
        UniqueConstraint("name", name="uq_portfolios_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # 2 decimal places: cash is genuinely counted in cents.
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="portfolio",
        order_by="Holding.symbol",
    )


class Holding(Base):
    """A long position in one symbol within one portfolio."""

    __tablename__ = "holdings"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_holdings_quantity_positive"),
        CheckConstraint(
            "average_buy_price > 0", name="ck_holdings_average_buy_price_positive"
        ),
        UniqueConstraint("portfolio_id", "symbol", name="uq_holdings_portfolio_id_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String(20))
    # 6 decimal places, so fractional shares remain representable later.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # 4 decimal places: this is an average, and averaging can produce more than
    # two decimals (100 / 3), so rounding to cents would quietly lose precision.
    average_buy_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))

    portfolio: Mapped["Portfolio"] = relationship(back_populates="holdings")
