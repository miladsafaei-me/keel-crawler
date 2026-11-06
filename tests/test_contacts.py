from keel_crawler.contacts import extract, extract_html, normalize_phone


def test_persian_digits_and_trunk_zero():
    found = extract("تماس: ۰۹۱۲ ۳۴۵ ۶۷۸۹", default_country="98")
    assert found.phones == ["+989123456789"]


def test_international_number_keeps_its_country():
    assert normalize_phone("+90 535 010 02 51", default_country="98") == "+905350100251"
    assert normalize_phone("0090 535 010 0251") == "+905350100251"


def test_year_range_and_short_numbers_are_not_phones():
    found = extract("از 1399-1403 و قیمت 450 تومان", default_country="98")
    assert found.phones == []


def test_handles_in_every_written_form():
    text = (
        "Instagram: @Turkish_Teacher\n"
        "پیج اینستاگرام: @second.page\n"
        "https://www.instagram.com/third_page/?hl=fa\n"
        "تلگرام: @my_channel  https://t.me/other_channel\n"
        "https://wa.me/989121112233"
    )
    found = extract(text)
    assert sorted(found.social["instagram"]) == ["second.page", "third_page", "turkish_teacher"]
    assert sorted(found.social["telegram"]) == ["my_channel", "other_channel"]
    assert found.social["whatsapp"] == ["+989121112233"]


def test_websites_hubs_and_emails():
    text = "سایت: www.turkishclass.ir  linktr.ee/teacher  ایمیل: Info@TurkishClass.ir"
    found = extract(text)
    assert found.emails == ["info@turkishclass.ir"]
    assert found.websites == ["https://www.turkishclass.ir"]
    assert found.link_hubs == ["https://linktr.ee/teacher"]


def test_post_links_are_not_profiles():
    found = extract("https://www.instagram.com/p/Cx12ab/ https://www.youtube.com/watch?v=abc")
    assert found.social.get("instagram", []) == []
    assert found.social.get("youtube", []) == []


def test_html_links():
    html = '<a href="mailto:a@b.com?subject=x">m</a><a href="tel:+98 21 1234 5678">t</a><p>hi</p>'
    found = extract_html(html, default_country="98")
    assert found.emails == ["a@b.com"]
    assert found.phones == ["+982112345678"]
