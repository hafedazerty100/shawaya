import os
from datetime import datetime, timezone

# Ensure we hit the Cloud DB
os.environ["DATABASE_URL"] = "postgresql://neondb_owner:npg_ow7fJUpb9eDg@ep-floral-shadow-ab5q05lj-pooler.eu-west-2.aws.neon.tech/coffee_24h?sslmode=require"
os.environ["APP_MODE"] = "server"

from app import create_app
from extensions import db
from models import Category, Product, Order, OrderItem, SyncLog

app = create_app("server")

with app.app_context():
    print("Clearing old data...")
    SyncLog.query.delete()
    OrderItem.query.delete()
    Order.query.delete()
    Product.query.delete()
    Category.query.delete()
    db.session.commit()

    print("Adding Arabic Categories...")
    cats_data = [
        ('قهوة ساخنة', 1),
        ('عصائر طازجة', 2),
        ('حلويات', 3)
    ]
    cats = {}
    for name, order in cats_data:
        c = Category(name=name, display_order=order)
        db.session.add(c)
        db.session.commit()
        cats[name] = c.id

    print("Adding Arabic Products with URLs...")
    now = datetime.now(timezone.utc)
    prods_data = [
        ('اسبريسو', 'قهوة اسبريسو غنية ومركزة', cats['قهوة ساخنة'], 250, 'espresso.png', True),
        ('كابتشينو', 'اسبريسو مع حليب مبخر ورغوة', cats['قهوة ساخنة'], 350, 'cappuccino.png', True),
        ('لاتيه', 'اسبريسو مع حليب مبخر وقليل من الرغوة', cats['قهوة ساخنة'], 350, 'latie.png', True),
        ('عصير برتقال', 'عصير برتقال طازج ومعصور', cats['عصائر طازجة'], 400, 'orange_juice.png', True),
        ('عصير ليمون بالنعناع', 'عصير منعش من الليمون والنعناع', cats['عصائر طازجة'], 300, 'lemon_mint.png', True),
        ('كيك شوكولاتة', 'قطعة كيك شوكولاتة غنية', cats['حلويات'], 450, 'chocolate_cake.png', True),
        ('تشيز كيك', 'تشيز كيك كلاسيكي', cats['حلويات'], 500, 'cheesecake.png', True),
    ]

    for name, desc, cid, price, img, active in prods_data:
        p = Product(
            name=name,
            description=desc,
            category_id=cid,
            price_cents=price,
            image=img,
            is_active=active,
        )
        db.session.add(p)
    
    db.session.commit()
    print("Database fully seeded with Arabic content!")
