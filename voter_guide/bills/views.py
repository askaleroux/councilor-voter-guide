# -*- coding: utf-8 -*-
import operator
from re import compile as _Re
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.db.models import Count, Q, F
from django.db import connections, IntegrityError, transaction

from councilors.models import CouncilorsDetail
from search.models import Keyword
from search.views import keyword_list, keyword_been_searched
from .models import Bills, Councilors_Bills
from candidates.models import Intent, Intent_Standpoints
from candidates.forms import Intent_StandpointsForm
from standpoints.models import Standpoints, User_Standpoint
from users.models import Achievements
from commontag.views import paginate
from .tasks import tag_create_achievement, tag_pro_achievement

_unicode_chr_splitter = _Re( '(?s)((?:[\ud800-\udbff][\udc00-\udfff])|.)' ).split

def split_unicode_chrs(text):
    return [chr for chr in _unicode_chr_splitter(text) if (chr!=' ' and chr)]

def select_county(request, index, county):
    regions = [
        {"region": "北部", "counties": ["臺北市", "新北市", "桃園市", "基隆市", "宜蘭縣", "新竹縣", "新竹市"]},
        {"region": "中部", "counties": ["苗栗縣", "臺中市", "彰化縣", "雲林縣", "南投縣"]},
        {"region": "南部", "counties": ["嘉義縣", "嘉義市", "臺南市", "高雄市", "屏東縣"]},
        {"region": "東部", "counties": ["花蓮縣", "臺東縣"]},
        {"region": "離島", "counties": ["澎湖縣", "金門縣", "連江縣"]}
    ]
    return render(request, 'bills/select_county.html', {'index': index, 'regions': regions})

def bills(request, county):
    query = Q(county=county)
    if request.GET.get('has_tag') == 'yes':
        query = query & Q(uid__in=Standpoints.objects.exclude(bill__isnull=True).values_list('bill_id', flat=True).distinct())
    elif request.GET.get('has_tag') == 'no':
        query = query & ~Q(uid__in=Standpoints.objects.exclude(bill__isnull=True).values_list('bill_id', flat=True).distinct())
    query = query & Q(uid__in=Standpoints.objects.filter(title=request.GET['tag']).exclude(bill__isnull=True).values_list('bill_id', flat=True).distinct()) if request.GET.get('tag') else query
    keyword = request.GET.get('keyword', '')
    constituency = request.GET.get('constituency')
    if keyword:
        bills = Bills.objects.filter(query & reduce(operator.and_, (Q(abstract__icontains=x) for x in split_unicode_chrs(keyword))))
        if bills:
            keyword_been_searched(keyword, 'bills', county)
    else:
        bills = Bills.objects.filter(query)
    if constituency and constituency != 'all':
        bills = bills.filter(uid__in=Councilors_Bills.objects.filter(petition=False, councilor__in=CouncilorsDetail.objects.filter(county=county, constituency=constituency).values_list('id', flat=True)).values_list('bill_id', flat=True))
    bills = bills.extra(
                     select={
                         'tags': "SELECT json_agg(row) FROM (SELECT title, pro FROM standpoints_standpoints su WHERE su.bill_id = bills_bills.uid ORDER BY su.pro DESC) row",
                     },
                 )\
                 .order_by('-election_year', '-uid')
    bills = paginate(request, bills)
    standpoints = Standpoints.objects.filter(county=county, bill__isnull=False).exclude(bill__isnull=True).values_list('title', flat=True).order_by('-pro').distinct()
    get_params = '&'.join(['%s=%s' % (x, request.GET[x]) for x in ['keyword', 'constituency', 'has_tag'] if request.GET.get(x)])
    return render(request, 'bills/bills.html', {'county': county, 'keyword_hot': keyword_list('bills', county), 'category': None, 'bills': bills, 'hot_standpoints': standpoints[:5], 'get_params': get_params})

def bill(request, bill_id):
    bill = get_object_or_404(Bills, uid=bill_id)
    intent = None
    if request.user.is_authenticated():
        try:
            intent = Intent.objects.get(user=request.user)
        except:
            instance = None
        else:
            try:
                instance = Intent_Standpoints.objects.get(intent=intent, bill_id=bill_id)
            except:
                instance = None
        if intent:
            form = Intent_StandpointsForm(instance=instance)
        if request.POST:
            with transaction.atomic():
                if request.POST.has_key('intent') and intent:
                    form = Intent_StandpointsForm(request.POST, instance=instance)
                    if form.has_changed() and form.is_valid():
                        intent_sp = form.save(commit=False)
                        intent_sp.intent = intent
                        intent_sp.bill = bill
                        intent_sp.save()
                else:
                    if request.POST.get('keyword', '').strip():
                        standpoint_id = u'bill-%s-%s' % (bill_id, request.POST['keyword'].strip())
                        standpoint, created = Standpoints.objects.get_or_create(uid=standpoint_id, county=bill.county, title=request.POST['keyword'].strip(), bill_id=bill_id, user=request.user)
                        if created:
                            User_Standpoint.objects.create(standpoint_id=standpoint_id, user=request.user)
                            Standpoints.objects.filter(uid=standpoint_id).update(pro=F('pro') + 1)
                            tag_create_achievement(request.user)
                            tag_pro_achievement(standpoint_id)
                    elif request.POST.get('pro'):
                        User_Standpoint.objects.create(standpoint_id=request.POST['pro'], user=request.user)
                        Standpoints.objects.filter(uid=request.POST['pro']).update(pro=F('pro') + 1)
                        tag_pro_achievement(request.POST['pro'])
                    elif request.POST.get('against'):
                        User_Standpoint.objects.get(standpoint_id=request.POST['against'], user=request.user).delete()
                        Standpoints.objects.filter(uid=request.POST['against']).update(pro=F('pro') - 1)

    c = connections['default'].cursor()
    c.execute(u'''
        select json_agg(row)
        from (
            select pro, json_agg(party_list) as party_list, sum(count)
            from (
                select pro, json_build_object('party', party, 'intents', intents, 'count', json_array_length(intents)) as party_list, json_array_length(intents) as count
                from (
                    select pro, party, json_agg(detail) as intents
                    from (
                        select pro, party, json_build_object('name', name, 'county', county, 'intent_id', intent_id, 'comment', comment) as detail
                        from (
                            select
                                cis.pro,
                                ci.party,
                                ci.name,
                                ci.county,
                                cis.intent_id,
                                cis.comment,
                                cis.create_at
                            FROM candidates_intent_standpoints cis
                            JOIN candidates_intent ci on ci.uid = cis.intent_id
                            WHERE cis.bill_id = %s
                            order by case
                                when ci.county = %s then 1
                                else 2
                            end, create_at
                        ) _
                    ) __
                    group by pro, party
                    order by pro, party
                ) ___
            ) ____
        group by pro
        order by sum desc
        ) row
    ''', [bill.uid, bill.county])
    r = c.fetchone()
    intent_sp_of_bill = r[0] if r else []
    standpoints_of_bill = Standpoints.objects.filter(bill_id=bill_id)\
                                             .order_by('-pro')
    if request.user.is_authenticated():
        standpoints_of_bill = standpoints_of_bill.extra(select={
            'have_voted': "SELECT true FROM standpoints_user_standpoint su WHERE su.standpoint_id = standpoints_standpoints.uid AND su.user_id = %s" % request.user.id,
        },)
    return render(request, 'bills/bill.html', {'county': bill.county, 'bill': bill, 'standpoints_of_bill': standpoints_of_bill, 'intent_sp_of_bill': intent_sp_of_bill, 'intent': intent, 'form': intent and form})
