"""Regression checks for partial matches, source evidence, and metadata recovery."""
from app.catalog_enrichment import enrich_record
from app.listing_explanations import concern_evidence, concern_reasons
from app.listing_quality import listing_quality, prioritize_listing_evidence, title_product_type
from app.product_search import ProductSearchRequest
from test_listing_enrichment import listing
import pytest


@pytest.mark.parametrize('title,family,expected', [
    ('Face Moisturizer Face Cream', 'skin_care', 'Moisturizer'),
    ('Sterling Silver Necklace Pendant', 'jewelry', 'Necklace'),
    ('Leather Crossbody Shoulder Bag Handbag', 'bags', 'Crossbody'),
    ('Shampoo and Conditioner Kit', 'hair_care', None),
    ('Shoe Charm', 'footwear', 'Shoe Charm'),
    ('Handbag Organizer Insert', 'bags', 'Handbag Organizer'),
])
def test_product_identity_aliases_and_accessories(title, family, expected):
    assert title_product_type(title, family) == expected


def test_metadata_recovers_recipient_and_coordinated_types_with_source():
    row = listing('Suitable for dry, sensitive and normal skin.', 'Hair type: Curly & Wavy.', '100% Cotton')
    row['title'] = "Women's Running Shoes"
    row['category'].update(domain='fashion', family='footwear')
    result = enrich_record(row, {})
    attrs = result['attributes']
    assert attrs['department']['value'] == "Women's"
    assert attrs['department']['field'] == '/title'
    assert attrs['skin_type']['value'] == 'dry, sensitive and normal'
    assert attrs['hair_type']['value'] == 'Curly & Wavy'
    assert attrs['material']['value'] == '100% Cotton'
    assert result['availability'] is None
    assert concern_evidence(result, ['dry skin'])[0]['support'] == 'positive'


def test_conflicting_material_is_not_invented_or_silently_accepted():
    row = enrich_record(listing('100% Cotton', 'Soft 60% cotton, 40% polyester blend.'), {})
    assert not row['attributes'].get('material')
    assert 'conflicting_material_composition' in row['quality']['warnings']
    quality = listing_quality(row, ProductSearchRequest(query='cotton shirt', product_goal='cotton shirt'), [])
    assert quality['cautions'][0]['code'] == 'conflicting_material'


def test_missing_and_ambiguous_recipient_stays_unknown():
    row = listing()
    row['category'].update(domain='fashion', family='jewelry')
    row['title'] = 'Necklace gift for women'
    assert not enrich_record(row, {})['attributes'].get('department')
    row['title'] = 'Men and Women Necklace'
    assert not enrich_record(row, {})['attributes'].get('department')


def test_explanation_finds_missed_brightness_but_not_ingredient_inference():
    row = listing('Reveals radiance and brightness.', 'Contains niacinamide.')
    item = concern_evidence(row, ['lightens my face'])[0]
    assert item['support'] == 'positive'
    assert item['evidence'][0]['text'] == 'Reveals radiance and brightness.'
    assert concern_evidence(listing('Contains niacinamide.'), ['lightens my face'])[0]['status'] == 'not_stated'


def test_selected_color_evidence_is_visible_in_product_reasons():
    from app.product_search import listing_evidence
    from app.commerce import explain_products
    from app.commerce_models import ShoppingTask
    row = listing()
    row['title'] = 'Lipstick shade 106'
    row['attributes']['color'] = {'value': 'Red, Lovely Rouge', 'field': '/details/Color', 'status': 'listing_claim'}
    row['listing_evidence'] = listing_evidence(row, 'red lipstick')
    explained = explain_products([row], ShoppingTask(id=1, query='red lipstick'))[0]
    assert any('Red, Lovely Rouge' in reason and '/details/Color' in reason for reason in explained['why_recommended'])


def test_negative_and_conflicting_mentions_are_not_positive_reasons():
    row = listing('Suitable for sensitive skin.', 'Not suitable for sensitive skin.')
    item = concern_evidence(row, ['sensitive skin'])[0]
    assert item['support'] == 'mixed'
    assert any(e['text'] == 'Not suitable for sensitive skin.' for e in item['evidence'])
    assert 'conflicting wording' in concern_reasons([item])[0]
    item = concern_evidence(listing('Not a brightening product.'), ['brighten my face'])[0]
    assert item['support'] == 'negated'
    assert 'negates this requirement' in concern_reasons([item])[0]
    item = concern_evidence(listing('Suitable for sensitive skin. Not suitable for sensitive skin.'), ['sensitive skin'])[0]
    assert item['support'] == 'mixed'
    item = concern_evidence(listing('No added fragrance, suitable for sensitive skin.'), ['sensitive skin'])[0]
    assert item['support'] == 'positive'


def test_ingredient_heading_and_single_ingredient_recovery_stays_literal():
    row = enrich_record(listing('Ingredients:', 'Water, Glycerin, Alcohol Denat.'), {})
    assert row['attributes']['ingredients']['value'] == 'Water, Glycerin, Alcohol Denat'
    assert row['attributes']['ingredients']['field'] == '/features/1'
    row = enrich_record(listing('Ingredients: Petrolatum.', 'Inactive Ingredients: Water, Glycerin.'), {})
    assert row['attributes']['ingredients']['value'] == 'Petrolatum'
    assert row['attributes']['inactive_ingredients']['value'] == 'Water, Glycerin'
    assert not enrich_record(listing('Ingredients:', 'Apply twice daily.'), {})['attributes'].get('ingredients')
    for text in ['All-day Colorcoat -', 'Formulated by Dr.', '>', 'Heart Bath Ball 1.77oz (50g)']:
        assert not enrich_record(listing('Ingredients:', text), {})['attributes'].get('ingredients')


def test_keyword_inventory_is_weak_not_supporting_evidence():
    text = 'shirts short sleeve shirts long sleeve shirts on sale shirts blue shirts black shirts red shirts white shirts large shirts small shirts pocket shirts fashion shirts slim shirts cheap shirts warm shirts cool'
    item = concern_evidence(listing(text), ['pocket'])[0]
    assert item['support'] == 'weak'
    text = 'mens shirts short sleeve casual long button down clearance polo big and tall under with pocket wrinkle free blue slim fit pockets flannel blacktshirt work price style dress shirt red sale prime medium small warm cotton pullover'
    assert concern_evidence(listing(text), ['with pockets'])[0]['support'] == 'weak'


def test_quality_prefers_supported_concerns_and_keeps_partial_matches_visible():
    request = ProductSearchRequest(query='brightening skin care', product_goal='skin care', concerns=['lightens my face', 'less chemical'])
    records = [listing('No benefit stated.'), listing('Brightening.'), listing('Brightening.')]
    records[2]['title'] = 'Facial Chemical Peel'
    rows = []
    for i, row in enumerate(records):
        evidence = concern_evidence(row, request.concerns)
        rows.append({'product_id': str(i), 'ranking': {}, 'listing_quality': listing_quality(row, request, evidence)})
    result = prioritize_listing_evidence({'method': 'test', 'results': rows}, request)
    assert [r['product_id'] for r in result['results']] == ['1', '0', '2']
    assert rows[1]['listing_quality']['unverified_concerns'] == 1
    assert rows[2]['listing_quality']['cautions'][0]['code'] == 'unrequested_format'


@pytest.mark.parametrize('goal,title,code', [
    ('handbag', 'Girls Mini Handbag', 'child_sizing'),
    ('lipstick', 'Lipstick Gift Set with Ring', 'bundle'),
    ('necklace', 'Personalized Nameplate Necklace', 'personalization'),
])
def test_unrequested_special_cases_rank_lower_but_explicit_requests_are_respected(goal, title, code):
    row = listing()
    row['title'] = title
    request = ProductSearchRequest(query=goal, product_goal=goal)
    assert code in {c['code'] for c in listing_quality(row, request, [])['cautions']}
    request.product_goal = title
    assert not listing_quality(row, request, [])['cautions']
