import re
import string
import random
from os import (listdir, stat, remove, environ)
from os.path import (isfile, join, getmtime)
from math import (sqrt, isnan)
from itertools import combinations
from datetime import (datetime, date, timedelta)
import time
import logging
import asyncio
from functools import lru_cache

from clint.textui import colored
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import cm
#for weights
#FIXME yahoo changes break ffn for portfolio siing
#import ffn
import quandl
from numpy import (power, where, maximum, sum, corrcoef, empty, exp, log, round)
from pandas import (read_pickle, to_datetime, DataFrame, concat, date_range, \
    read_csv, DatetimeIndex)
#FIXME seaborn has deprecations
import seaborn as sns

import warnings
warnings.simplefilter("ignore")

from django.db import IntegrityError
from django.core.mail import send_mail
from django.template.defaultfilters import slugify
from django.conf import settings

from . import easywebdav
from .mysql_utils import create_symbol, mysql_connect_db #, _signals_to_mysql
from .models import (Symbols, Brokers, Periods, Stats, Systems, QtraUser, \
    Signals, Corr)
from server.server import conn

brokers = Brokers.objects.all()
ignored_symbols = ['AI50', 'M1']


@lru_cache(maxsize=None)
def filenames_list(path_to):
    p = join(settings.DATA_PATH, path_to)
    return [f for f in listdir(p) if isfile(join(p, f))]


async def file_cleaner(filename):
    try:
        remove(filename)
        if settings.SHOW_DEBUG:
            print("Removed failing file from data folder {}.".format(filename))
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At file cleaning {}".format(e))


@lru_cache(maxsize=1024*10)
def nonasy_read_df(filename):
    df = read_pickle(filename)
    #make index
    #df.index = to_datetime(df.index).to_pydatetime()
    return df


@lru_cache(maxsize=1024*10)
async def read_df(filename):
    df = read_pickle(filename)
    return df


async def fitness_rank(average_trade, trades, gross_profit, gross_loss):
    try:
        if trades > 0:
            if gross_loss < 0:
                if gross_profit > 0:
                    if average_trade != 0:
                        rank_ = (float(average_trade) * (1.0 - \
                            1.0/sqrt(float(trades)))*float(\
                            gross_profit)/abs(float(gross_loss)))
                    else:
                        rank_ = 0.0
                else:
                    rank_ = 0.0
            else:
                rank_ = (float(average_trade) * (1.0 - 1.0/sqrt(float(trades)))*float(gross_profit))
        else:
            rank_ = 0.0
    except Exception as e:
        print("At fitness rank {}".format(e))
        rank_ = 0.0

    return rank_ / 1000.0


def get_portf_d(df_cumsum, df_mae, df_trade, list_mae, list_returns, margin, fr, list_margin, list_portfolio):
    list_mae.append(df_mae)
    list_returns.append(df_trade)
    list_margin.append(margin)
    list_portfolio.append(df_cumsum)
    fr += fr

    return (list_portfolio, list_mae, list_returns, list_margin, fr)


@lru_cache(maxsize=1024*10)
def get_strategies(broker):
    if broker:
        strategies = Stats.objects.select_related('symbol', 'period', 'broker', \
            'system').filter(sharpe__gt=settings.MIN_MACHINE_SHARPE, trades__gt=40, \
            win_rate__gt=settings.MIN_MACHINE_WIN_RATE, broker=broker \
            ).exclude(avg_trade__isnull=True, trades__isnull=True, \
            symbol__commission__isnull=True).order_by('sharpe').reverse().values('symbol__symbol', \
            'symbol__currency', 'direction', 'period__name', \
            'period__period', 'period', 'system', 'system__title', 'broker__slug', \
            'sharpe', 'avg_trade', 'avg_win', 'avg_loss', 'win_rate', \
            'trades', 'fitness', 'symbol__description', 'symbol__commission', \
            'max_dd', 'intraday_dd', 'symbol__margin_initial', 'symbol', \
            'symbol__broker__title', 'broker', 'symbol__symbol')
    else:
        strategies = Stats.objects.select_related('symbol', 'period', 'broker', \
            'system').filter(sharpe__gt=settings.MIN_MACHINE_SHARPE, trades__gt=40, \
            win_rate__gt=settings.MIN_MACHINE_WIN_RATE \
            ).exclude(avg_trade__isnull=True, trades__isnull=True, \
            symbol__commission__isnull=True).order_by('sharpe').reverse().values('symbol__symbol', \
            'symbol__currency', 'direction', 'period__name', \
            'period__period', 'period', 'system', 'system__title', 'broker__slug', \
            'sharpe', 'avg_trade', 'avg_win', 'avg_loss', 'win_rate', \
            'trades', 'fitness', 'symbol__description', 'symbol__commission', \
            'max_dd', 'intraday_dd', 'symbol__margin_initial', 'symbol', \
            'symbol__broker__title', 'broker', 'symbol__symbol')
    return strategies


async def save_quandl_file(sym, quandl_periods):
    symbol = sym.split('/')[1]
    for p in quandl_periods:
        if not (p[0] is None):
            data = quandl.get(sym, collapse=p[0])
        else:
            data = quandl.get(sym)
        filename = join(settings.DATA_PATH, "quandl", "{0}=={1}.mp".format(symbol, p[1]))
        data.to_pickle(path=filename)


def quandl_process(loop):
    quandl.ApiConfig.api_key = settings.QUANDL_API_KEY
    quandl_symbols = ["YAHOO/INDEX_VIX", "CBOE/VXV"]
    quandl_periods = [("monthly", 43200), (None, 1440), ("weekly", 10080)]

    loop.run_until_complete(asyncio.gather(*[save_quandl_file(\
        sym=sym, quandl_periods=quandl_periods) for sym in quandl_symbols], \
        return_exceptions=True
    ))


async def make_strat_image(system, symbol, period, broker, filename, data):
    path_to_performance = join(settings.DATA_PATH, 'performance')
    mdpi = 300

    try:
        title = "{0} on {1} {2} [{3}]".format(system, symbol, period, broker)

        if len(data) > 0:
            data.rename(columns={'LONG_PL_CUMSUM': 'Strategy, longs', \
                'SHORT_PL_CUMSUM': 'Strategy, shorts', 'LONG_DIFF_CUMSUM': \
                'Buy & hold', 'SHORT_DIFF_CUMSUM': 'Short & hold'}, inplace=True)

            plt.figure(figsize=(3840/mdpi, 2160/mdpi), dpi=mdpi)
            data['Strategy, longs'].plot(x='Date', y='Value, $', legend=True, \
                style='r', lw=3).scatter(x=data.index, \
                y=data.LONG_MAE, label='MAE', color='DarkGreen')
            data['Buy & hold'].plot(legend=True, style='g').set_title(title+', Longs')
            plt.axhline(y=0.0)
            plt.savefig(join(settings.STATIC_ROOT, 'collector', 'images', \
                'meta', '{0}=={1}=={2}=={3}==longs.png'.format(broker, symbol, period, system)))
            plt.close()

            plt.figure(figsize=(3840/mdpi, 2160/mdpi), dpi=mdpi)
            data['Strategy, shorts'].plot(x='Date', y='Value, $', legend=True, \
                style='r', lw=3).scatter(x=data.index, \
                y=data.SHORT_MAE, label='MAE', color='DarkGreen')
            data['Short & hold'].plot(legend=True, style='g').set_title(title+', Shorts')
            plt.axhline(y=0.0)
            plt.savefig(join(settings.STATIC_ROOT, 'collector', 'images', \
                'meta', '{0}=={1}=={2}=={3}==shorts.png'.format(broker, symbol, period, system)))
            plt.close()

            plt.figure(figsize=(3840/mdpi, 2160/mdpi), dpi=mdpi)
            data['Strategy, longs & shorts'] = data['Strategy, shorts'] + \
                data['Strategy, longs']
            data['Strategy, longs & shorts'].plot(x='Date', \
                y='Value, $', legend=True, style='r', \
                lw=3).scatter(x=data.index, y=(data.SHORT_MAE+data.LONG_MAE), \
                label='MAE', color='DarkGreen')
            data['Buy & hold'].plot(legend=True, style='g'\
                ).set_title(title+', Longs & shorts')
            plt.axhline(y=0.0)
            plt.savefig(join(settings.STATIC_ROOT, 'collector', 'images', \
                'meta', '{0}=={1}=={2}=={3}==longs_shorts.png'.format(broker, \
                symbol, period, system)))
            plt.close()
            if settings.SHOW_DEBUG:
                print("Made images for: {}\n".format(filename))
    except Exception as e:
        print("At make_strat_image {}".format(e))


async def make_image(path_to, filename):
    try:
        spl = filename.split('==')
        broker = spl[0]
        broker_slugified = slugify(broker).replace('-', '_')
        symbol = spl[1]
        period = spl[2]
        system = spl[3].split('.')[0]

        image_filename = join(settings.STATIC_ROOT, 'collector', 'images', \
            'meta', '{0}=={1}=={2}=={3}==longs_shorts.png'.format(broker_slugified, \
            symbol, period, system))

        data = await read_df(join(path_to, filename))
        data = data.loc[data['CLOSE'] != 0]

        if not isfile(image_filename):
            await make_strat_image(system=system, symbol=symbol, period=period, broker=broker_slugified, filename=filename, data=data)
        if datetime.fromtimestamp(getmtime(image_filename)) < (datetime.now() - timedelta(days=30)):
            await make_strat_image(system=system, symbol=symbol, period=period, broker=broker_slugified, filename=filename, data=data)

    except Exception as e:
        print("At making images {}\n".format(e))


def make_images(loop):
    path_to = join(settings.DATA_PATH, "performance")
    filenames = filenames_list(path_to="performance")

    loop.run_until_complete(asyncio.gather(*[make_image(path_to=path_to, \
        filename=filename) for filename in filenames], \
        return_exceptions=True
    ))



async def write_corr(symbol_a, symbol_b, corr):
    try:
        try:
            c = Corr.objects.create(symbol_a=symbol_a, symbol_b=symbol_b, value=corr)
            c.save()
        except:
            c = Corr.objects.get(symbol_a=symbol_a, symbol_b=symbol_b)
            c.value = corr
            c.save()
    except Exception as e:
        print(colored.red("At making corr: {}\n".format(e)))


async def process_corr(subset, dates):
    try:
        s1 = Symbols.objects.get(symbol=subset[0])
        s2 = Symbols.objects.get(symbol=subset[1])
        f1 = "{0}=={1}==1440.mp".format(s1.broker.title, s1.symbol)
        f2 = "{0}=={1}==1440.mp".format(s2.broker.title, s2.symbol)

        df1 = read_pickle(join(settings.DATA_PATH, f1))
        df2 = read_pickle(join(settings.DATA_PATH, f2))

        data = concat([df1.DIFF, df2.DIFF], axis=1, join_axes=[DataFrame(index=dates).index]).fillna(0.0)
        data.columns = ['A', 'B']
        data = data.loc[data['A'] != 0]
        data = data.loc[data['B'] != 0]
        corr = round(corrcoef(data['A'].as_matrix(), data['B'].as_matrix())[0,1], 4)
        await write_corr(symbol_a=s1, symbol_b=s2, corr=corr)
    except Exception as e:
        print(colored.red("At process_corr {}".format(e)))


def generate_correlations(loop):
    filenames = filenames_list(path_to="")
    symbols = Symbols.objects.filter().exclude(symbol__in=ignored_symbols)
    symbols_list = [symbol.symbol for symbol in symbols]
    combinated = combinations(symbols_list, 2)
    dates = date_range(end=date(datetime.now().year, datetime.now().month, datetime.now().day),periods=20*252, freq='D', name='DATE_TIME', tz=None)

    loop.run_until_complete(asyncio.gather(*[process_corr(subset=subset, \
        dates=dates) for subset in combinated], \
        return_exceptions=True
    ))


async def convert_to_csv(path_to, filename):
    try:
        out = join(path_to, 'csv', filename+'.csv')
        df = read_pickle(join(path_to, filename))
        df.to_csv(out)
        print("CSVed into {}\n".format(out))
    except Exception as e:
        print(e)


def pickle_to_svc(folder, loop):
    if folder == 'incoming':
        path_to = join(settings.DATA_PATH, '')
    else:
        path_to = join(settings.DATA_PATH, folder)

    filenames = filenames_list(path_to=path_to)

    loop.run_until_complete(asyncio.gather(*[convert_to_csv(path_to=path_to, \
        filename=filename) for filename in filenames], \
        return_exceptions=True
    ))


def qindex(broker):
    try:
        stats = Stats.objects.select_related('symbol', 'period', 'broker', \
            'system').filter(sharpe__gt=settings.SHARPE, \
            trades__gt=settings.MIN_TRADES, broker=broker, win_rate__gt=settings.WIN_RATE \
            ).exclude(system__title='AI50', avg_trade__isnull=True, avg_trade__lt=0.0, trades__isnull=True, \
            symbol__commission__isnull=True, sortino__lt=0.2).exclude(symbol__symbol='AI50').order_by('sortino').reverse()[:50]
    except Exception as e:
        print(" At qindex {}".format(e))
    return stats


async def collect_idx_dfs(df):
    return df


def generate_qindex(loop):
    for broker in brokers:
        print("Going to make index for {}".format(broker.title))
        idx = qindex(broker=broker)
        print("Got {} frames for index".format(len(idx)))

        df_out = loop.run_until_complete(asyncio.gather(*[collect_idx_dfs(df=_get_df(\
            broker_slug=i.broker.slug, symbol=i.symbol.symbol, period=i.period.period, \
            system=i.system.title, folder='performance')) for i in idx],
            return_exceptions=True
        ))

        dates = date_range(end=date(datetime.now().year, datetime.now().month, datetime.now().day),periods=20*252, freq='D', name='DATE_TIME', tz=None)
        df = concat(df_out, axis=1) #, join_axes=[dates])

        try:
            df.rename(columns={'SHORT_PL_CUMSUM': 'LONG_PL_CUMSUM',
                'SHORT_PL': 'LONG_PL',
                'SHORT_TRADES': 'LONG_TRADES',
                'SHORT_MAE': 'LONG_MAE'
                }, inplace=True)

            #df = df.fillna(0.0)

            pl = sum(df.LONG_PL, axis=1)
            diff = sum(df.DIFF, axis=1)
            diff_cums = sum(df.LONG_DIFF_CUMSUM.fillna(method='ffill'), axis=1)
            cums = sum(df.LONG_PL_CUMSUM.fillna(method='ffill'), axis=1)
            margin = sum(df.LONG_MARGIN, axis=1)
            mae = sum(df.LONG_MAE.fillna(method='ffill'), axis=1)
            trades = sum(df.LONG_TRADES, axis=1)

            d = concat([pl/100.0, margin/100.0, mae/100.0, cums/100.0, diff/100.0, diff_cums/100.0, trades], axis=1)
            d.columns = ['LONG_PL', 'LONG_MARGIN', 'LONG_MAE', 'LONG_PL_CUMSUM', 'DIFF', 'DIFF_CUMSUM', 'LONG_TRADES']

            filename = join(settings.DATA_PATH, 'portfolios', '{}_qndx.mp'.format(broker.slug))
            d.to_pickle(path=filename)
            print(colored.green("Wrote qindex at {}".format(filename)))
        except Exception as e:
            print(colored.red("At generate_qindex {}".format(e)))


def cumulate_returns(x):
    try:
        val = x.cumsum()[-1]
    except:
        pass
        val = 0
    return val


async def aggregate_returns(returns, convert_to):
    if convert_to == 'weekly':
        return returns.groupby(
            [lambda x: x.year,
             lambda x: x.month,
             lambda x: x.isocalendar()[1]]).apply(cumulate_returns)
    elif convert_to == 'monthly':
        return returns.groupby(
            [lambda x: x.year, lambda x: x.month]).apply(cumulate_returns)
    elif convert_to == 'yearly':
        return returns.groupby(
            [lambda x: x.year]).apply(cumulate_returns)
    else:
        print('convert_to must be weekly, monthly or yearly')


async def write_y(returns, image_filename):
    try:
        ax = plt.gca()
        ax.yaxis.grid(linestyle=':')

        ret_plt = await aggregate_returns(returns, 'yearly') #* 100.0
        ret_plt.plot(kind="bar")
        ax.set_title('Yearly Returns, %', fontweight='bold')
        ax.set_ylabel('')
        ax.set_xlabel('')
        ax.set_xticklabels(ax.get_xticklabels(), rotation=45)
        ax.xaxis.grid(False)
        plt.savefig(image_filename)
        plt.close()
        print(colored.green("Wrote yearly graph {}".format(image_filename)))
    except Exception as e:
        print(colored.red("At write_y".format(e)))


async def make_yearly_returns(returns, broker_slugified, symbol, period, system, direction):
    image_filename = join(settings.STATIC_ROOT, 'collector', 'images', \
        'yearly', '{0}=={1}=={2}=={3}=={4}.png'.format(broker_slugified, \
        symbol, period, system, direction))

    if not (isfile(image_filename)):
        await write_y(returns=returns, image_filename=image_filename)
    if datetime.fromtimestamp(getmtime(image_filename)) < (datetime.now() - timedelta(days=30)):
        await write_y(returns=returns, image_filename=image_filename)


async def save_qindex_heatmap(data, image_filename):
    try:
        monthly_ret = await aggregate_returns(returns=data, convert_to='monthly')
        monthly_ret = monthly_ret.unstack()
        monthly_ret = round(monthly_ret, 3)
        monthly_ret.rename(
            columns={1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr',
                     5: 'May', 6: 'Jun', 7: 'Jul', 8: 'Aug',
                     9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'},
            inplace=True
        )
        ax = plt.gca()

        sns.heatmap(
            monthly_ret.fillna(0), # * 100.0,
            annot=True,
            fmt="0.1f",
            annot_kws={"size": 8},
            alpha=1.0,
            center=0.0,
            cbar=False,
            cmap=cm.RdYlGn,
            ax=ax)
        ax.set_title('A.I. Returns, %', fontweight='bold')
        #ax.set_ylabel('')
        #ax.set_yticklabels(ax.get_yticklabels(), rotation=0)
        #ax.set_xlabel('')

        plt.savefig(image_filename)
        plt.close()
        print(colored.green("Wrote heatmap image for {}\n".format(image_filename)))
    except Exception as e:
        print("At save_qindex_heatmap {}".format(e))


async def convert_to_perc(data, broker, symbol, period, system, direction):
    try:
        stats = Stats.objects.get(broker__slug=broker, symbol__symbol=symbol, \
            period__period=period, system__title=system, direction=direction)
        acc_min = stats.acc_minimum
        if settings.SHOW_DEBUG:
            print("Account minimum {}".format(acc_min))
        p = (data / float(acc_min)) * 100.0
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At convert_to_perc {}".format(e))
        p = None

    return p


async def qindex_heatmap(broker_slugified, symbol='AI50', period=1440, system='AI50', direction='longs'):
    try:
        filename = join(settings.DATA_PATH, 'portfolios', '{}_qndx.mp'.format(broker_slugified))
        image_filename = join(settings.STATIC_ROOT, 'collector', 'images', \
            'heatmap', '{0}=={1}=={2}=={3}=={4}.png'.format(broker_slugified, \
            symbol, period, system, direction))
        data = await read_df(filename=filename)

        returns = await convert_to_perc(data=data.last('108M').LONG_PL, \
            broker=str(broker_slugified), symbol='AI50', period=1440, \
            system='AI50', direction=1)
        returns.columns = ['LONG_PL']

        if not isfile(image_filename):
            await save_qindex_heatmap(data=returns, image_filename=image_filename)
        if datetime.fromtimestamp(getmtime(image_filename)) < (datetime.now() - timedelta(days=30)):
            await save_qindex_heatmap(data=returns, image_filename=image_filename)
        await make_yearly_returns(returns=returns, broker_slugified=broker_slugified, \
            symbol=symbol, period=period, system=system, direction=direction)
    except Exception as e:
        print("At qindex_heatmap {}".format(e))


async def write_h(image_filename, data):
    try:
        monthly_ret = await aggregate_returns(returns=data, convert_to='monthly')
        monthly_ret = monthly_ret.unstack()
        monthly_ret = round(monthly_ret, 3)
        monthly_ret.rename(
            columns={1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr',
                     5: 'May', 6: 'Jun', 7: 'Jul', 8: 'Aug',
                     9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'},
            inplace=True
        )
        ax = plt.gca()

        sns.heatmap(
            monthly_ret.fillna(0), # * 100.0,
            annot=True,
            fmt="0.1f",
            annot_kws={"size": 8},
            alpha=1.0,
            center=0.0,
            cbar=False,
            cmap=cm.RdYlGn,
            ax=ax)
        ax.set_title('Returns heatmap, %', fontweight='bold')

        plt.savefig(image_filename)
        plt.close()
        print("Wrote heatmap image for {}\n".format(image_filename))
    except Exception as e:
        print(colored.red("At write_h".format(e)))


async def save_heatmap(data, broker_slugified, symbol, period, system, direction):
    try:
        image_filename = join(settings.STATIC_ROOT, 'collector', 'images', \
            'heatmap', '{0}=={1}=={2}=={3}=={4}.png'.format(broker_slugified, \
            symbol, period, system, direction))

        if not isfile(image_filename):
            await write_h(image_filename=image_filename, data=data)
        else:
            if datetime.fromtimestamp(getmtime(image_filename)) < (datetime.now() - timedelta(days=30)):
                await write_h(image_filename=image_filename, data=data)
    except Exception as e:
        print("At save_heatmap {}".format(e))


async def make_heat_img(path_to, filename):
    try:
        spl = filename.split("==")
        broker = str(slugify(spl[0])).replace("-", "_")
        symbol = spl[1]
        period = spl[2]
        system = spl[3].split(".")[0]

        df = read_pickle(join(path_to, filename))

        if len(df.index) > settings.MIN_TRADES:
            longs = await convert_to_perc(data=df.LONG_PL, broker=broker, \
                symbol=symbol, period=int(period), system=system, direction=1)
            shorts = await convert_to_perc(data=df.SHORT_PL, broker=broker, \
                symbol=symbol, period=int(period), system=system, direction=2)
            long_short = await convert_to_perc(data=(df.LONG_PL + df.SHORT_PL), \
                broker=broker, symbol=symbol, period=int(period), \
                system=system, direction=0)

            if not (longs is None):
                await save_heatmap(data=longs, \
                    broker_slugified=broker, symbol=symbol, period=period, \
                    system=system, direction='longs')
                await make_yearly_returns(returns=longs, broker_slugified=broker, \
                    symbol=symbol, period=period, system=system, direction='longs')
            if not (shorts is None):
                await save_heatmap(data=shorts, \
                    broker_slugified=broker, symbol=symbol, period=period, \
                    system=system, direction='shorts')
                await make_yearly_returns(returns=shorts, broker_slugified=broker, \
                    symbol=symbol, period=period, system=system, direction='shorts')
            if not (long_short is None):
                await save_heatmap(data=long_short, \
                    broker_slugified=broker, symbol=symbol, period=period, \
                    system=system, direction='longs_shorts')
                await make_yearly_returns(returns=long_short, broker_slugified=broker, \
                    symbol=symbol, period=period, system=system, direction='longs_shorts')

    except Exception as e:
        print("At make_heat_img {}".format(e))


def generate_monthly_heatmaps(loop):
    path_to = join(settings.DATA_PATH, "performance")
    filenames = filenames_list(path_to="performance")

    loop.run_until_complete(asyncio.gather(*[make_heat_img(\
        path_to=path_to, filename=filename) for filename in filenames], \
        return_exceptions=True
    ))

    #AI50 index heatmap
    loop.run_until_complete(asyncio.gather(*[qindex_heatmap(broker_slugified=broker.slug) for broker in brokers],
        return_exceptions=True
    ))


@lru_cache(maxsize=1024*10)
def _get_df(broker_slug, symbol, period, system, folder, limit=False):
    path_to_history = join(settings.DATA_PATH, folder)

    try:
        broker = Brokers.objects.get(slug=broker_slug)
        wh = join(path_to_history, "{0}=={1}=={2}=={3}.mp".format(broker.title, symbol, period, system))
        df = read_pickle(wh)
        #df.index = to_datetime(df.index).to_pydatetime()
        if limit:
            return df.last(settings.LIMIT_MONTHS)
        else:
            return df
    except Exception as e:
        print("At get df {}".format(e))
        return DataFrame()


async def expand(records):
    d = [dict(r.items()) for r in records]
    return d


@lru_cache(maxsize=1024*10)
async def asy_get_df(con, broker_slug, symbol, period, system, folder, limit=False):
    path_to_history = join(settings.DATA_PATH, folder)

    broker = await con.fetch("""SELECT title FROM collector_brokers WHERE slug='{}'""".format(broker_slug))
    await con.close()

    try:
        #broker = Brokers.objects.get(slug=broker_slug)
        wh = join(path_to_history, "{0}=={1}=={2}=={3}.mp".format(dict(broker[0])['title'], symbol, period, system))
        df = read_pickle(wh)
        #df.index = to_datetime(df.index).to_pydatetime()
        if limit:
            return df.last(settings.LIMIT_MONTHS)
        else:
            return df
    except Exception as e:
        print("At asy_get_df {}".format(e))
        return DataFrame()


async def update_signal_sent_status(user, strategy, direction, df, i):
    try:
        #update that email is sent!
        s = Signals.objects.get(user=user, broker=strategy.broker, symbol=strategy.symbol,
            system=strategy.system, period=strategy.period, direction=direction,
            date_time=df.ix[i].name)
        if settings.SHOW_DEBUG:
            print("Got something from signals {}\n".format(s))
        s.sent_email = True
        s.save()
        if settings.SHOW_DEBUG:
            print("Updated signal status")
    except Exception as e:
        print(colored.red("At signal status update {}".format(e)))


async def send_signal(strategy, strings, df, i, user, direction):
    try:
        #if settings.SHOW_DEBUG:
        print("Trying to send signal from df {}...".format(df.ix[i]))
        if direction == 1:
            direction_name = "Longs only"
        elif direction == 2:
            direction_name = "Shorts only"
        else:
            direction_name = "Longs and shorts"

        try:
            s = Signals.objects.filter(user=user, broker=strategy.broker,
                symbol=strategy.symbol, system=strategy.system, period=strategy.period,
                direction=direction, date_time=df.ix[i].name,
                sent_email=False).count()
            print("Signals that aren't sent for this day: {}\n".format(s))
        except Exception as e:
            print("At send_signal collecting the list {}".format(e))

        if s != 0:
            if len(str(user.email)) > 0:
                recipients = [user.email]
                #recipients = ['talaikis.tadas@gmail.com']
                print("Recipinet: {}".format(recipients))
                subject = "{0} on Quantrade Trading Signals".format(strings[0])
                # FIXME as systems send separate signals for longs and shoerts,
                # it is impossible to show correct url for 'longs_shorts' for
                # strategy at this time
                #url = "https://quantrade.co.uk/{0}/{1}/{2}/{3}/{4}/".format(p.broker.slug, p.symbol.symbol, p.period.period, p.system.title, strings[1])
                #message = "{0}: {1} for {2} ({3}) - {4}. Using system: {5} ({6}). Size: {7}\n\nSystem URL:{8}\n\n".format(df.ix[i].name, strings[0], p.symbol.symbol, p.broker.title, p.period.name, p.system.title, direction_name, 1, url)
                message = "{0}: {1} for {2} ({3}) - {4}. Using system: {5} ({6}). Size: {7}\n\n".format(df.ix[i].name, strings[0], strategy.symbol.symbol, strategy.broker.title, strategy.period.name, strategy.system.title, direction_name, 1)
                sender = settings.DEFAULT_FROM_EMAIL

                try:
                    send_mail(subject, message, sender, recipients)
                    print("Signal sent for {}.".format(user.email))
                except Exception as e:
                    print(colored.red("At send_signal actual sending {}".format(e)))

                await update_signal_sent_status(user=user, strategy=strategy, direction=direction, df=df, i=i)
    except Exception as e:
        print(colored.red("At sending signal: {}\n".format(e)))


async def update_returns(user, strategy, direction, date_time, perf):
    try:
        if settings.SHOW_DEBUG:
            print("Trying to update returns")

        signl = Signals.objects.get(user=user, broker=strategy.broker, symbol=strategy.symbol,
            period=strategy.period, system=strategy.system, direction=direction,
            date_time=date_time)

        df = perf.ix[date_time]

        if settings.SHOW_DEBUG:
            print("Got from db {}".format(signl))
            print("Got this for performance {}".format(df))
            print()

        if direction == 1:
            #if settings.SHOW_DEBUG:
                #print(df.shift(-1).ix[i].LONG_PL)
            #signl.returns = float(df.shift(-1).ix[i].LONG_PL)
            signl.returns = float(df.LONG_PL)
            signl.save()
            if settings.SHOW_DEBUG:
                print("Updated for result then: {0} {1}.".format(df.ix[i].name, df.ix[i].LONG_PL))
        elif direction == 2:
            #if settings.SHOW_DEBUG:
                #print(df.shift(-1).ix[i].SHORT_PL)
            signl.returns = float(df.SHORT_PL)
            signl.save()
            if settings.SHOW_DEBUG:
                print("Updated for result then: {0} {1}.".format(df.ix[i].name, df.ix[i].SHORT_PL))
    except Exception as e:
        print(colored.red("At update_returns {}\n".format(e)))


async def save_signal(user, strategy, df, i, perf, direction):
    try:
        signl = Signals.objects.create(user=user, broker=strategy.broker, symbol=strategy.symbol,
            period=strategy.period, system=strategy.system, direction=direction,
            date_time=df.ix[i].name, returns=None)
        signl.save()
        #if settings.SHOW_DEBUG:
        print(colored.green("Signal saved."))
    except Exception as e:
        await update_returns(user=user, strategy=strategy, direction=direction, date_time=df.ix[i].name, perf=perf.shift(-1))
        if settings.SHOW_DEBUG:
            print(colored.red("At save_signal {}".format(e)))


async def _save_signals(df, perf, strategy, user, period):
    try:
        if settings.SHOW_DEBUG:
            print("Trying to save signals for {}".format(strategy.system.title))

        for i in range(0,len(df.index)):
            now = date.today()
            ye = now.year
            mo = now.month
            d = now.day
            dow = now.weekday()
            prev_d = []

            #calculate one day before this
            if (d >= 1) & (d < 31):
                next_day = d + 1
                prev_mo = mo
            else:
                if mo == any([1, 4, 6, 8, 9, 11]):
                    prev_d = [31]
                elif mo == any([5, 7, 10, 12]):
                    prev_d = [30]
                else:
                    prev_d = [28, 29]

                if mo == 1:
                    prev_mo = 12
                else:
                    prev_mo = mo - 1

            df['ts'] = df.index
            df['ts'] = to_datetime(df['ts'])

            #save signals before sending
            if strategy.direction == 1:
                if df.ix[i].BUY_SIDE == 1:
                    if settings.SHOW_DEBUG:
                        print("Trying to save buy side...")
                    await save_signal(user=user, strategy=strategy, df=df, i=i, perf=perf, direction=1)
            elif strategy.direction == 2:
                if df.ix[i].SELL_SIDE == 1:
                    if settings.SHOW_DEBUG:
                        print("Trying to save sell side...")
                    await save_signal(user=user, strategy=strategy, df=df, i=i, perf=perf, direction=2)

            #send signal only if it is today!
            if( ((df['ts'].ix[-1].to_pydatetime().year == ye) & (df['ts'].ix[-1].to_pydatetime().month == mo) & (df['ts'].ix[-1].to_pydatetime().day == d)) | \
                #for month signals
                \
                ((df['ts'].ix[-1].to_pydatetime().year == ye) & (df['ts'].ix[-1].to_pydatetime().month == mo) & (df['ts'].ix[-1].to_pydatetime().day == d) & (period == 43200)) | \
                 #for week signals
                 \
                 ((df['ts'].ix[-1].to_pydatetime().year == ye) &  (df['ts'].ix[-1].to_pydatetime().month == mo) & (df['ts'].ix[-1].to_pydatetime().day == any(prev_d)) &  (df['ts'].ix[-1].to_pydatetime().weekday() == 6) & (dow == 0) & (period == 10080)) | \
                 ((df['ts'].ix[-1].to_pydatetime().year == ye) &  (df['ts'].ix[-1].to_pydatetime().month == prev_mo) & (df['ts'].ix[-1].to_pydatetime().day == any(prev_d)) &  (df['ts'].ix[-1].to_pydatetime().weekday() == 6) & (dow == 0) & (period == 10080)) ):
                    print("This day when signal should be sent!")

                    #if settings.SHOW_DEBUG:
                    print()
                    print("DF year {}\n".format(df['ts'].ix[-1].to_pydatetime().year))
                    print("Now year {}\n".format(ye))
                    print("DF month {}\n".format(df['ts'].ix[-1].to_pydatetime().month))
                    print("Now month {}\n".format(mo))
                    print("DF day {}\n".format(df['ts'].ix[-1].to_pydatetime().day))
                    print("Now day {}\n".format(d))

                    if strategy.direction == 1:
                        if df.ix[i].BUY_SIDE == 1:
                            #if settings.SHOW_DEBUG:
                            print("Trying to send buy side...\n")
                            await send_signal(strategy=strategy, strings=['BUY', 'longs'], df=df, i=i, user=user, direction=1)
                    elif strategy.direction == 2:
                        if df.ix[i].SELL_SIDE == 1:
                            #if settings.SHOW_DEBUG:
                            print("Trying to send sell side...\n")
                            await send_signal(strategy=strategy, strings=['SELL', 'shorts'], df=df, i=i, user=user, direction=2)
    except Exception as e:
        print(colored.red("At _save_signals {}\n".format(e)))


async def mask_signals(usr, signals):
    try:
        if usr.username == settings.MACHINE_USERNAME:
            if settings.SHOW_DEBUG:
                print("A.K.A. Main user")
            fk_dt = datetime(2016, 10, 1)
            mask = (to_datetime(signals.index).to_pydatetime() >= fk_dt)
        else:
            #mask = (to_datetime(signals.index).to_pydatetime() >= usr.date_joined)
            fk_dt = settings.SINCE_WHEN
            mask = (to_datetime(signals.index).to_pydatetime() >= fk_dt)

        #signals.index = to_datetime(signals.index).to_pydatetime()
        if settings.SHOW_DEBUG:
            print("Masked signals")
            print(signals.tail())
    except Exception as e:
        print(colored.red("At mask_signals {}".format(e)))

    return signals[mask]


async def user_signals(usr, strategies):
    try:
        print("Processing signals for {}".format(usr))
        if len(strategies) > 0:
            for strategy in strategies:
                if settings.SHOW_DEBUG:
                    print("For strategy from AI strategies {}".format(strategy.system.title))
                signals = await asy_get_df(con=await conn(), broker_slug=strategy.broker.slug, \
                    symbol=strategy.symbol.symbol, period=strategy.period.period, \
                    system=strategy.system.title, folder="systems", limit=False)
                perf = await asy_get_df(con=await conn(), broker_slug=strategy.broker.slug, \
                    symbol=strategy.symbol.symbol, period=strategy.period.period, \
                    system=strategy.system.title, folder="performance", limit=False)

                if settings.SHOW_DEBUG:
                    print(signals.tail())

                signals = await mask_signals(usr=usr, signals=signals)
                perf = await mask_signals(usr=usr, signals=perf)
                await _save_signals(df=signals, perf=perf, strategy=strategy, user=usr, period=strategy.period.period)

                #for the case when signals delivered through mysql
                #_signals_to_mysql(db=db, df=signals, p=p, user=usr, direction=1)
    except Exception as e:
        print(colored.red("At user_signals {}".format(e)))


def generate_signals(loop):
    users = QtraUser.objects.all()
    if settings.SHOW_DEBUG:
        print("Got users {}".format(users))
    #db = mysql_connect_db()

    for broker in brokers:
        #list of index strategies
        print("Making signals for {}".format(broker))
        strategies = qindex(broker=broker)

        loop.run_until_complete(asyncio.gather(*[user_signals(usr=usr, strategies=strategies) for usr \
            in users], return_exceptions=True
        ))


@asyncio.coroutine
def gather_bad_file(loop, filename, path_to, list_failing):
    try:
        if settings.SHOW_DEBUG:
            print("Checking {}\n".format(filename))
        y = datetime.now().year
        m = datetime.now().month
        d = datetime.now().day
        h = datetime.now().hour

        dow = datetime.now().weekday()
        if '1440' in filename:
            df = read_csv(filepath_or_buffer=join(path_to, filename), sep=',', delimiter=None, \
                header=0, names=None, index_col=0, usecols=None, squeeze=False, prefix=None, \
                mangle_dupe_cols=True, dtype=None, engine=None, \
                converters=None, true_values=None, false_values=None, \
                skipinitialspace=False, skiprows=None, nrows=None, \
                na_values=None, keep_default_na=True, na_filter=True, \
                verbose=False, skip_blank_lines=True, parse_dates=False, \
                infer_datetime_format=False, keep_date_col=False, \
                date_parser=None, dayfirst=False, iterator=False, chunksize=None, \
                compression='infer', thousands=None, decimal='.', lineterminator=None, \
                quotechar='"', quoting=0, escapechar=None, comment=None, \
                encoding=None, dialect=None, tupleize_cols=False, \
                error_bad_lines=True, warn_bad_lines=True, skipfooter=0, \
                skip_footer=0, doublequote=True, delim_whitespace=False, \
                as_recarray=False, compact_ints=False, use_unsigned=False, \
                low_memory=False, buffer_lines=None, memory_map=False, \
                float_precision=None)

            #make index
            df.sort_index(axis=0, ascending=True, inplace=True)
            #df.index = to_datetime(df.index).to_pydatetime()
            df['ts'] = df.index
            df['ts'] = to_datetime(df['ts'])
            df.index.name = "DATE_TIME"

            if dow in range(6):
                if h > 12 & h < 22:
                    if (df['ts'].ix[-1].to_pydatetime().year == y) & \
                            (df['ts'].ix[-1].to_pydatetime().month == m) & \
                            (df['ts'].ix[-1].to_pydatetime().day != d):
                        list_failing.append([filename, "DOW: {}".format(dow), \
                            "Day: {}\n".format(d), "DataFrame day: {}".format(\
                            df['ts'].ix[-1].to_pydatetime().day )])

    except Exception as e:
        #if settings.SHOW_DEBUG:
        print("At gather_bad_file {}".format(e))
        try:
            list_failing.append([filename, "DOW: {}".format(dow), "Day: {}".format(d), \
                "Day from DataFrame: {}\n".format(df['ts'].ix[-1].to_pydatetime().day)])
        except:
            list_failing.append([filename, 'Empty'])
    return list_failing


def read_failing(filenames, path_to, loop, list_failing):
    try:
        cor = loop.run_until_complete(asyncio.gather(*[gather_bad_file(loop=loop, \
            filename=filename, path_to=path_to, list_failing=list_failing) \
            for filename in filenames])
            )
        if settings.SHOW_DEBUG:
            print("Func read_failing res {}".format(cor))
    except Exception as e:
        print("At read_failing {}".format(e))

    return list_failing


@asyncio.coroutine
def clean_failed_file(path_to, file_name):
    try:
        if settings.SHOW_DEBUG:
            print("Deleting: {}\n".format(file_name[0]))
            remove(join(path_to, file_name[0]))
    except Exception as e:
        #if settings.SHOW_DEBUG:
        print("At deleting: {}\n".format(e))


def data_checker(loop):
    list_failing = []
    p = join(settings.DATA_PATH, "incoming")
    filenames = filenames_list(path_to="incoming")

    list_failing = read_failing(filenames=filenames, path_to=p, loop=loop, list_failing=list_failing)

    if settings.SHOW_DEBUG:
        print("Failing symbols: {}\n".format(list_failing))

    cnt = len(list_failing)
    print("Failing number: {}\n".format(cnt))

    if (cnt > 0) & (cnt < 10):
        subject = "Failing datafiles: {}".format(cnt)
        message = "{0}\n\n".format(list_failing)
        sender = settings.DEFAULT_FROM_EMAIL
        send_mail(subject, message, sender, settings.NOTIFICATIONS_EMAILS)

        loop.run_until_complete(asyncio.gather(*[clean_failed_file(path_to=p, \
            file_name=file_name) for file_name in list_failing], return_exceptions=True
        ))


def clean_folder(path_to):
    filenames = filenames_list(path_to=path_to)

    for filename in filenames:
        remove(join(path_to, filename))


async def update_stats(broker, symbol, period, system, direction, stats):
    try:
        s = Stats.objects.get(broker=broker, symbol=symbol, \
            period=period, system=system, direction=direction) #.delete()

        s.sharpe = stats['sharpe']
        s.bh_sharpe = stats['bh_sharpe']
        s.std = stats['std']
        s.var = stats['var']
        s.avg_trade = stats['avg_trade']
        s.avg_win = stats['avg_win']
        s.avg_loss = stats['avg_loss']
        s.win_rate = stats['win_rate']
        s.trades = stats['trades']
        s.fitness = stats['fr']
        s.intraday_dd = stats['intraday_dd']
        s.total_profit = stats['total_profit']
        s.max_dd = stats['max_dd']
        s.yearly = stats['yearly']
        s.yearly_p = stats['avg_yearly_p']
        s.acc_minimum = stats['acc_minimum']
        s.sortino = stats['sortino']
        s.bh_sortino = stats['bh_sortino']
        s.save()

        #if settings.SHOW_DEBUG:
        print("Updated stats for {}\n".format(symbol))
    except Exception as e:
        print(colored.red("At update stats {0} with {1} {2}".format(e, symbol, system)))


async def std_func(data):
    try:
        std = data.loc[data != 0].std()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At std func {}\n".format(e))
        std = 0
    return std


async def var_func(data):
    try:
        var = data.loc[data != 0].var()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At var func {}\n".format(e))
        avg_trade = 0
    return var


async def avg_trade_func(data):
    try:
        avg_trade = data.loc[data != 0].mean()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At avg trade func {}\n".format(e))
        avg_trade = 0
    return avg_trade


async def lpm(returns, threshold, order):
    """This method returns a lower partial moment of the returns
    Create an array he same length as returns containing the minimum return threshold"""
    threshold_array = empty(len(returns))
    threshold_array.fill(threshold)
    # Calculate the difference between the threshold and the returns
    diff = threshold_array - returns
    # Set the minimum of each to 0
    diff = diff.clip(min=0)
    # Return the sum of the different to the power of order
    return sum(diff ** order) / len(returns)


async def sortino_ratio(returns, target=0):
    expected_return = returns.mean()
    risk_free = 0.06
    return (expected_return - risk_free) / sqrt(await lpm(returns, target, 2))


async def sharpe_func(avg_trade, std):
    try:
        if not (avg_trade is None):
            if not (std is None):
                sharpe = avg_trade / std
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At sharpe func {}\n".format(e))
        sharpe = 0.0
    return sharpe


async def avg_win_loss_func(data):
    try:
        avg_win = data.loc[data > 0].mean()
        avg_loss = data.loc[data < 0].mean()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At avg win func {}\n".format(e))
        avg_win = 0.0
        avg_loss = 0.0
    return (avg_win, avg_loss)


async def win_rate_func(data):
    try:
        win_rate = float(data.loc[data > 0].count()) / data.loc[data != 0].count()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At win rate func {}\n".format(e))
        win_rate = 0.0
    return win_rate


async def trades_func(data):
    try:
        trades = int(data.loc[data == 1].count())
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At trades func {}\n".format(e))
        trades = 0

    return trades


async def fr_func(avg_trade, trades, returns, cumsum):
    try:
        gross_profit = returns.loc[returns > 0].sum()
        gross_loss = abs(returns.loc[returns < 0].sum())
        total_profit = cumsum.ix[-1]
        fr = await fitness_rank(average_trade=avg_trade, trades=trades, \
            gross_profit=gross_profit, gross_loss=gross_loss)
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("fr_func {}\n".format(e))
        fr = 0.0
        gross_profit = 0,0
        gross_loss = 0.0
        total_profit = 0.0
    return (gross_profit, gross_loss, total_profit, fr)


async def max_dd_func(data):
    try:
        max_y = maximum.accumulate(data)
        dd = data - max_y
        max_dd = abs(dd.min())
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("max_dd_func {}\n".format(e))
        max_dd = None
    return max_dd


async def idd_func(df, direction):
    try:
        if direction == 1:
            filtered = df.loc[df['LONG_MAE'] != 0]
            intraday_dd = (filtered['LONG_PL_CUMSUM'] - filtered['LONG_MAE']).max()
        elif direction == 2:
            filtered = df.loc[df['SHORT_MAE'] != 0]
            intraday_dd = (filtered['SHORT_PL_CUMSUM'] - filtered['SHORT_MAE']).max()
        else:
            filtered = df.loc[(df['SHORT_MAE']+df['LONG_MAE']) != 0]
            intraday_dd = ((filtered['SHORT_PL_CUMSUM']+filtered['LONG_PL_CUMSUM']) - (filtered['SHORT_MAE']+filtered['LONG_MAE'])).max()
    except Exception as e:
        if settings.SHOW_DEBUG:
            print("At idd_func {}\n".format(e))
        intraday_dd = None
    return intraday_dd


async def yearly_func(total_profit, years, max_dd, intraday_dd, symbol, margin=0.0):
    try:
        yearly = total_profit / years
        if not(symbol is None):
            acc_minimum = max_dd + intraday_dd + float(symbol.margin_initial)
        else:
            acc_minimum = max_dd + intraday_dd + margin
        if acc_minimum != 0:
            avg_yearly_p = (yearly * 100.0) / (acc_minimum * 2.0)
        else:
            avg_yearly_p = 0.0
    except Exception as e:
        #if settings.SHOW_DEBUG:
        print("At yearly func {}\n".format(e))
        #yearly = 0.0
        #acc_minimum = None
        #avg_yearly_p = 0.0

    return (yearly, acc_minimum, avg_yearly_p)


def error_email(e):
    if settings.NOTIFICATIONS_ENABLED:
        send_mail('ERROR', '{}.\n\n'.format(e), \
            settings.DEFAULT_FROM_EMAIL,
            settings.NOTIFICATIONS_EMAILS, \
            fail_silently=True)


async def get_stats(df, direction, years, symbol):
    try:
        stats = {}
        if direction == 1:
            returns = df['LONG_PL']
            diff = df['DIFF']
            trades = df['LONG_TRADES']
            cumsum = df['LONG_PL_CUMSUM']
            margin = df['LONG_MARGIN'].max()
        elif direction == 2:
            returns = df['SHORT_PL']
            diff = -df['DIFF']
            trades = df['SHORT_TRADES']
            cumsum = df['SHORT_PL_CUMSUM']
            margin = df['SHORT_MARGIN'].max()
        else:
            returns = df['BUYSELL']
            diff = df['DIFF']
            trades = df['SHORT_TRADES'] + df['LONG_TRADES']
            cumsum = df['BUYSELL_CUMSUM']
            margin = (df['LONG_MARGIN'] + df['SHORT_MARGIN']).max()

        stats['std'] = await std_func(data=returns)
        stats['margin'] = margin
        stats['var'] = await var_func(data=returns)
        stats['avg_trade'] = await avg_trade_func(data=returns)
        stats['sharpe'] = await sharpe_func(avg_trade=stats['avg_trade'], std=stats['std'])
        stats['sortino'] = await sortino_ratio(returns=returns.as_matrix(), target=0)
        stats['bh_std'] = await std_func(data=diff)
        stats['bh_avg_trade'] = await avg_trade_func(data=diff)
        stats['bh_sharpe'] = await sharpe_func(avg_trade=stats['bh_avg_trade'], std=stats['bh_std'])
        stats['bh_sortino'] = await sortino_ratio(returns=diff.as_matrix(), target=0)
        stats['avg_win'], stats['avg_loss'] = await avg_win_loss_func(data=returns)
        stats['win_rate'] = await win_rate_func(data=returns)
        stats['trades'] = await trades_func(data=trades)
        stats['gross_profit'], stats['gross_loss'], stats['total_profit'], \
            stats['fr'] = await fr_func(avg_trade=stats['avg_trade'], \
            trades=stats['trades'], returns=returns, cumsum=cumsum)
        stats['max_dd'] = await max_dd_func(data=cumsum)
        stats['intraday_dd'] = await idd_func(df=df, direction=direction)
        stats['yearly'], stats['acc_minimum'], stats['avg_yearly_p'] = await \
            yearly_func(total_profit=stats['total_profit'], years=years, \
            max_dd=stats['max_dd'], intraday_dd=stats['intraday_dd'], symbol=symbol)
    except Exception as e:
        print(colored.red("At get_stats {}".format(e)))

    if settings.SHOW_DEBUG:
        print("Got stats {}".format(stats))

    return stats


async def write_stats(stats, broker, symbol, period, system, direction):
    try:
        if stats['trades'] >= settings.MIN_TRADES:
            try:
                s = Stats.objects.create(broker=broker, symbol=symbol, period=period, \
                    system=system, direction=direction, sharpe=stats['sharpe'], \
                    std=stats['std'], var=stats['var'], avg_trade=stats['avg_trade'], \
                    avg_win=stats['avg_win'], avg_loss=stats['avg_loss'], \
                    win_rate=stats['win_rate'], trades=stats['trades'], fitness=stats['fr'], \
                    intraday_dd=stats['intraday_dd'], max_dd=stats['max_dd'], \
                    total_profit=stats['total_profit'], yearly=stats['yearly'], yearly_p=stats['avg_yearly_p'], \
                    acc_minimum=stats['acc_minimum'], bh_sharpe=stats['bh_sharpe'], \
                    sortino=stats['sortino'], bh_sortino=stats['bh_sortino'])
                s.save()
                print(colored.green("Wrote new stats for {}".format(symbol)))
            except IntegrityError:
                pass
            except Exception as e:
                print(colored.red("At write_stats {}".format(e)))
                await update_stats(broker=broker, symbol=symbol, period=period, \
                    system=system, direction=direction, stats=stats)
    except Exception as e:
        print(colore.red("At writing stats {}".format(e)))


async def stats_process(df, d, years, broker, symbol, period, system):
    if settings.SHOW_DEBUG:
        print("Processing stats for {0} {1}".format(symbol, system))
    try:
        stats = await get_stats(df=df, direction=d, years=years, \
            symbol=symbol)
        await write_stats(stats=stats, broker=broker, symbol=symbol, \
            period=period, system=system, direction=d)
    except Exception as e:
        print(colored.red("At stats_process {}".format(e)))


async def loop_over_strats(path_to, filename, loop):
    try:
        look_for = join(path_to, filename)
        if settings.SHOW_DEBUG:
            print("Stats Working with {}".format(look_for))

        spl = filename.split('==')
        broker_ = spl[0]
        symbol_ = spl[1]
        period_ = spl[2]
        system_ = spl[3].split('.')[0]

        symbol = Symbols.objects.get(symbol=symbol_)
        period = Periods.objects.get(period=period_)
        try:
            if settings.SHOW_DEBUG:
                print("System: {}".format(system_))
            system = Systems.objects.get(title=system_)
        except:
            system = None

        if system:
            broker = Brokers.objects.get(title=broker_)
            df = await read_df(look_for)

            try:
                years = df.index[-1].year - df.index[0].year + ((12 - df.index[0].month) + df.index[-1].month) / 12.0
            except Exception as e:
                print("At years {}\n".format(e))
                years = None

            if not (years is None):
                df['BUYSELL'] = df['LONG_PL'] + df['SHORT_PL']
                df['BUYSELL_CUMSUM'] = df['BUYSELL'].cumsum()

                directions = [0, 1, 2]
                for d in directions:
                    await stats_process(df=df, d=d, years=years, broker=broker, \
                        symbol=symbol, period=period, system=system)
    except Exception as e:
        print(colored.red("At loop over strats {}\n".format(e)))
        await file_cleaner(filename=look_for)


async def generate_qindexd_stats(broker):
    try:
        if settings.SHOW_DEBUG:
            print("trying to generate Qindex stats")

        filename = join(settings.DATA_PATH, 'portfolios', '{}_qndx.mp'.format(broker.slug))
        df = await read_df(filename=filename)
        df =df.last('108M')

        years = df.index[-1].year - df.index[0].year + ((12 - df.index[0].month) + df.index[-1].month) / 12.0
        stats = {}

        try:
            symbol = Symbols.objects.get(symbol='AI50')
        except:
            symbol = Symbols.objects.create(symbol='AI50')

        period = Periods.objects.get(period=1440)
        try:
            system = Systems.objects.get(title='AI50')
        except:
            system = Systems.objects.create(title='AI50')

        stats['margin'] = df.LONG_MARGIN.max()
        stats['std'] = await std_func(data=df.LONG_PL)
        stats['var'] = await var_func(data=df.LONG_PL)
        stats['avg_trade'] = await avg_trade_func(data=df.LONG_PL)
        stats['sharpe'] = await sharpe_func(avg_trade=stats['avg_trade'], std=stats['std'])
        stats['sortino'] = await sortino_ratio(returns=df.LONG_PL.as_matrix(), target=0)
        stats['bh_std'] = await std_func(data=df.DIFF)
        stats['bh_avg_trade'] = await avg_trade_func(data=df.DIFF)
        stats['bh_sharpe'] = await sharpe_func(avg_trade=stats['bh_avg_trade'], std=stats['bh_std'])
        stats['bh_sortino'] = await sortino_ratio(returns=df.DIFF.as_matrix(), target=0)
        stats['avg_win'], stats['avg_loss'] = await avg_win_loss_func(data=df.LONG_PL)
        stats['win_rate'] = await win_rate_func(data=df.LONG_PL)
        stats['trades'] = await trades_func(data=df.LONG_TRADES)
        stats['gross_profit'], stats['gross_loss'], stats['total_profit'], \
            stats['fr'] = await fr_func(avg_trade=stats['avg_trade'], \
            trades=stats['trades'], returns=df.LONG_PL, cumsum=df.LONG_PL_CUMSUM)
        stats['max_dd'] = await max_dd_func(data=df.LONG_PL_CUMSUM)
        stats['intraday_dd'] = await idd_func(df=df, direction=1)
        stats['yearly'], stats['acc_minimum'], stats['avg_yearly_p'] = await yearly_func(\
            total_profit=stats['total_profit'], years=years, \
            max_dd=stats['max_dd'], intraday_dd=stats['intraday_dd'], symbol=None, margin=stats['margin'])
        if settings.SHOW_DEBUG:
            print(stats)

        try:
            s = Stats.objects.create(broker=broker, symbol=symbol, period=period, \
                system=system, direction=1, sharpe=stats['sharpe'], \
                std=stats['std'], var=stats['var'], avg_trade=stats['avg_trade'], \
                avg_win=stats['avg_win'], avg_loss=stats['avg_loss'], \
                win_rate=stats['win_rate'], trades=stats['trades'], fitness=stats['fr'], \
                intraday_dd=stats['intraday_dd'], max_dd=stats['max_dd'], \
                total_profit=stats['total_profit'], yearly=stats['yearly'], yearly_p=stats['avg_yearly_p'], \
                acc_minimum=stats['acc_minimum'], bh_sharpe=stats['bh_sharpe'], \
                sortino=stats['sortino'], bh_sortino=stats['bh_sortino'])
            s.save()
            symbol.margin_initial = stats['margin']
            symbol.save()
            print(colored.green("Wrote Qindex stats to db"))
        except Exception as e:
            if settings.SHOW_DEBUG:
                print(colored.red("When trying to save Qindex stats {}".format(e)))
            await update_stats(broker=broker, symbol=symbol, period=period, system=system, \
                direction=1, stats=stats)
    except Exception as e:
        print("At generate_qindexd_stats {}".format(e))


def generate_stats(loop):
    path_to = join(settings.DATA_PATH, "performance")
    filenames = filenames_list(path_to="performance")

    loop.run_until_complete(asyncio.gather(*[loop_over_strats(\
        path_to=path_to, filename=filename, loop=loop) for filename \
        in filenames], return_exceptions=True
    ))

    loop.run_until_complete(asyncio.gather(*[generate_qindexd_stats(broker=broker) for broker in brokers],
        return_exceptions=True
     ))


def create_periods():
    data = settings.PUBLIC_PERIODS

    for per in data:
        try:
            p = Periods.objects.create(period=per[0], name=per[1])
            p.save()
            print("Created period: {}\n".format(per[0]))
        except IntegrityError:
            pass
        except Exception as e:
            print("[ERROR] At period creation: {0}\n".format(e))


async def create_broker(_broker):
    try:
        broker = Brokers.objects.create(title=_broker)
        broker.save()
        if settings.SHOW_DEBUG:
            print("Broker created.")
    except IntegrityError:
        pass
    except Exception as e:
        print("At create_bhroker {}\n".format(e))

    return _broker


def create_symbol_postgres(name, broker):
    try:
        if len(broker) > 0:
            if len(name) > 0:
                broker_id = Brokers.objects.get(title=broker)
                symbol = Symbols.objects.create(symbol=name, broker=broker_id)
                symbol.save()
                print("Created symbol at Postgres.")
    except IntegrityError:
        pass
    except Exception as e:
        print("At creating symbol: {}\n".format(e))


async def get_commission(symb):
    symbol = Symbols.objects.select_related('broker').filter(symbol=symb).values('commission')

    if symbol.count() > 0:
        try:
            value = float(symbol[0]['commission'])
        except Exception as e:
            if settings.SHOW_DEBUG:
                print("At getting commission {}\n".format(e))
            value = None
    else:
        value = None

    return value


async def data_downloader(item, webdav):
    try:
        from_filename = item[0]
        to_filename = from_filename[3:].replace('%20', ' ')
        if 'DATA_MODEL' in to_filename:
            f = open(join(settings.DATA_PATH, 'incoming', to_filename), 'w')
            webdav.download(from_filename, f)
            f.close()
            if settings.SHOW_DEBUG:
                print("File downloaded {}".format(to_filename))
    except Exception as e:
        print("At generate remote file {}\n".format(e))


def generate_remote_files(loop):
    for resource in settings.WEBDAV_SOURCES:
        webdav = easywebdav.Client(host=resource[0], port=resource[1], username=settings.WEBDAV_USERNAME, password=settings.WEBDAV_PASSWORD)
        filelist = webdav.ls()

        loop.run_until_complete(asyncio.gather(*[data_downloader(item=item, webdav=webdav \
            ) for item in filelist], return_exceptions=True
        ))


async def key_gen(user):
    if not user.key:
        user.key = ''.join(random.choice(chars) for _ in range(size))
        user.save()
        print("Generated key for: {}\n".format(user))


def generate_keys(loop):
    size = settings.USER_KEY_SYMBOLS
    chars = string.ascii_uppercase + string.digits + string.ascii_lowercase

    users = QtraUser.objects.filter(user_type=1)

    loop.run_until_complete(asyncio.gather(*[key_gen(user=user) \
        for user in users], return_exceptions=True
    ))


async def create_sym(filename):
    #spl = str(filename.filename).split('_')
    spl = filename.split('_')
    broker_ = spl[2]
    symbol = spl[3]

    broker = await create_broker(_broker=broker_)
    create_symbol(name=symbol, broker=broker)
    create_symbol_postgres(name=symbol, broker=broker)


def create_special_symbol(sym, broker):
    create_symbol(name=sym, broker=broker)
    create_symbol_postgres(name=sym, broker=broker)


def create_symbols(loop):
    filenames = filenames_list(path_to="incoming")

    loop.run_until_complete(asyncio.gather(*[create_sym(\
        filename=filename) for filename in filenames],
        return_exceptions=True
    ))

    syms = ['M1', 'AI50']
    broker = 'Ava Trade EU Ltd.'
    for sym in syms:
        create_special_symbol(sym=sym, broker=broker)


async def get_currency(currency, broker_name, period):
    df = None
    if currency == 'AUD':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'AUDUSD', period)
            df = read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At getting currency file {}\n".format(e))
            df = None
    elif currency == 'CAD':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDCAD', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'GBP':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'GBPUSD', period)
            df = read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'JPY':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDJPY', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'HUF':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDHUF', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'DKK':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDDKK', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print(e)
            df = None
    elif currency == 'NOK':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDNOK', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'NZD':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'NZDUSD', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'ILS':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDILS', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'SEK':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDSEK', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except:
            df = None
    elif currency == 'TRY':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDTRY', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'RUB':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDRUB', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'PLN':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDPLN', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'CZK':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDCZK', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'CNH':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDCNH', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'THB':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDTHB', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'CNY':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDCNY', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'CHF':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDCHF', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'ZAR':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDZAR', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'SGD':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDSGD', period)
            df = 1.0 / (read_pickle(join(settings.DATA_PATH, filename)))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'HKD':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDHKD', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'MXN':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'USDMXN', period)
            df = 1.0 / read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'EUR':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'EURUSD', period)
            df = read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'PNC':
        try:
            filename = "{0}=={1}=={2}.mp".format(broker_name, 'GBPUSD', period)
            df = read_pickle(join(settings.DATA_PATH, filename))
        except Exception as e:
            print("At get_currency {0} with {1}".format(e, currency))
            df = None
    elif currency == 'USD':
        df = 1.0

    #if not (df is None):
        #try:
            #df.index = to_datetime(df.index).to_pydatetime()
        #except:
            #pass

    return df


async def process_commissions(symbol, multiplied_symbols):
    try:
        symbol_ = Symbols.objects.filter(symbol=symbol).values('currency', 'spread', 'digits', 'tick_size', 'tick_value', 'broker', 'symbol')
        if settings.SHOW_DEBUG:
            print("Processing commisions for {}".format(symbol_))

        if any(symbol_[0]['symbol'] in s for s in multiplied_symbols):
            value = (((power(10.0, -symbol_[0]['digits']) * \
                float(symbol_[0]['spread'])) / float(symbol_[0]['tick_size'])) * \
                float(symbol_[0]['tick_value'])) * 100.0
        else:
            value = (((power(10.0, -symbol_[0]['digits']) * \
                float(symbol_[0]['spread'])) / float(symbol_[0]['tick_size'])) * \
                float(symbol_[0]['tick_value']))

        symbol.commission = value
        symbol.save()
    except Exception as e:
        print("At process commissions {}".format(e))
        symbol.commission = None
        symbol.save()
    if settings.SHOW_DEBUG:
        print("Updated commision value for {0}\n".format(symbol.symbol))


def create_commissions(loop):
    multiplied_symbols = ['LTCMini', 'LTCWeekly']
    symbols = Symbols.objects.filter().exclude(symbol__in=ignored_symbols)

    loop.run_until_complete(asyncio.gather(*[process_commissions(symbol=symbol, \
        multiplied_symbols=multiplied_symbols) for symbol in symbols],
        return_exceptions=True
    ))


async def adjustment_bureau(data, symbol_name, broker_name, period_name):
    try:
        broker = Brokers.objects.get(title=broker_name)
        symbol = Symbols.objects.filter(symbol=symbol_name, broker=broker).values('symbol', 'tick_size', 'tick_value', 'currency')

        if symbol.count() > 0:
            curr = await get_currency(currency=symbol[0]['currency'], broker_name=broker_name, period=period_name)
        else:
            curr = None

        if not (curr is None):
            try:
                tick_size = float(symbol[0]['tick_size'])
                tick_value = float(symbol[0]['tick_value'])
                tmp = ((data / tick_size) * tick_value)

                value = tmp * curr.CLOSE
                #print "Made currency adjusted df {0}.".format(value.dropna().head())
            except Exception as e:
                if settings.SHOW_DEBUG:
                    print("At making adjustment bureau {0}. Going for original.".format(e))
                value = ((data / tick_size) * tick_value)
        else:
            value = None
    except Exception as e:
        print("At adjustment bureau {0} with {1}".format(e, symbol))
        value = None

    return value


async def process_symbols_to_postgress(row):
    try:
        if settings.SHOW_DEBUG:
            print(row)
        ignored = ['M1', 'AI50']
        if not (any(s in row[6] for s in ignored)):
            symbol = Symbols.objects.get(symbol=row[6])
            broker = Brokers.objects.get(title=row[10])

            symbol.description = row[7]
            symbol.spread = row[0]
            symbol.tick_size = row[1]
            symbol.tick_value = row[2]
            symbol.digits = row[3]
            symbol.currency = row[4]
            symbol.price = row[9]
            symbol.profit_type = row[5]
            symbol.margin_initial = row[8]
            symbol.broker = broker
            symbol.save()
            if settings.SHOW_DEBUG:
                print("Updated Postgres symbol data for {}\n".format(row[6]))
    except Exception as e:
        print("At process_symbols_to_postgress symbols {0} with {1}".format(e, row))


def symbol_data_to_postgres(dbsql, loop):
    c = dbsql.cursor()
    query = "USE {0};".format(settings.MYSQL_DATABASE)
    c.execute(query)
    query = "SELECT spread, tick_size, tick_value, digits, profit_currency, \
        profit_calc, symbol, description, margin_initial, price_at_calc_time, \
        broker FROM collector_symbols;"
    c.execute(query)
    res = c.fetchall()

    loop.run_until_complete(asyncio.gather(*[process_symbols_to_postgress(row=row) for row in res],
        return_exceptions=True
    ))


async def image_to_db(path_to, s):
    try:
        partial_path = 'static/collector/images/'

        if s.direction == 1:
            dir_slug = 'longs'
        if s.direction == 2:
            dir_slug = 'shorts'
        if s.direction == 0:
            dir_slug = 'longs_shorts'

        f = "{0}=={1}=={2}=={3}=={4}.png".format(s.broker.slug, s.symbol.symbol, s.period.period, s.system.title, dir_slug)
        if isfile(join(path_to, 'meta', f)):
            filename = "https://quantrade.co.uk/{0}meta/{1}=={2}=={3}=={4}=={5}.png".format(partial_path, s.broker.slug, s.symbol.symbol, s.period.period, s.system.title, dir_slug)
            s.img = filename
            print(colored.green("Wrote images urls to db for {}".format(filename)))
        if isfile(join(path_to, 'heatmap', f)):
            filename = "https://quantrade.co.uk/{0}heatmap/{1}=={2}=={3}=={4}=={5}.png".format(partial_path, s.broker.slug, s.symbol.symbol, s.period.period, s.system.title, dir_slug)
            s.heatmap = filename
            print(colored.green("Wrote images urls to db for {}".format(filename)))
        if isfile(join(path_to, 'yearly', f)):
            filename = "https://quantrade.co.uk/{0}yearly/{1}=={2}=={3}=={4}=={5}.png".format(partial_path, s.broker.slug, s.symbol.symbol, s.period.period, s.system.title, dir_slug)
            s.yearly_ret = filename
            print(colored.green("Wrote images urls to db for {}".format(filename)))

        strategy_url = "https://quantrade.co.uk/{0}/{1}/{2}/{3}/{4}/".format(s.broker.slug, s.symbol.symbol, s.period.period, s.system.title, dir_slug)
        s.strategy_url = strategy_url

        s.save()
    except Exception as e:
        print(colored.red("At writing urls {}".format(e)))


def process_urls_to_db(loop):
    path_to = join(settings.STATIC_ROOT, 'collector', 'images')

    stats = Stats.objects.all()

    loop.run_until_complete(asyncio.gather(*[image_to_db(path_to=path_to, s=s) for s in stats],
        return_exceptions=True
    ))


"""
def portf_weights(data):
    try:
        d = ffn.get(tickers=[], provider=None, common_dates=False, forward_fill=False, \
            clean_tickers=False, column_names=None, ticker_field_sep=':', mrefresh=False, \
            existing=data)
        weights = d.calc_mean_var_weights(covar_method='ledoit-wolf', options={'maxiter': 5000000, 'disp': True })
    except Exception as e:
        print(colored.red("At portf_weights: {}\n".format(e)))
        weights = None

    return weights
"""

def min_variance(loop):
    broker = Brokers.objects.get(slug='ava_trade_eu_ltd')
    idx = qindex(broker=broker)
    df_out, syms = [], []

    for i in idx:
        if i.direction == 1:
            df_out.append(_get_df(broker_slug=i.broker.slug, symbol=i.symbol.symbol, period=i.period.period, \
                system=i.system.title, folder='performance').LONG_PL_CUMSUM.resample('D').pad())
            syms.append("{0}_{1}_{2}_1".format(i.symbol.symbol, i.period.period, i.system.title))
        elif i.direction == 2:
            df_out.append(_get_df(broker_slug=i.broker.slug, symbol=i.symbol.symbol, period=i.period.period, \
                system=i.system.title, folder='performance').SHORT_PL_CUMSUM.resample('D').pad())
            syms.append("{0}_{1}_{2}_2".format(i.symbol.symbol, i.period.period, i.system.title))
        else:
            df_out.append(_get_df(broker_slug=i.broker.slug, symbol=i.symbol.symbol, period=i.period.period, \
                system=i.system.title, folder='performance').LONG_PL_CUMSUM.resample('D').pad())
            syms.append("{0}_{1}_{2}_0".format(i.symbol.symbol, i.period.period, i.system.title))
            df_out.append(_get_df(broker_slug=i.broker.slug, symbol=i.symbol.symbol, period=i.period.period, \
                system=i.system.title, folder='performance').SHORT_PL_CUMSUM.resample('D').pad())
            syms.append("{0}_{1}_{2}_0".format(i.symbol.symbol, i.period.period, i.system.title))

    returns_df = concat(df_out, axis=1).fillna(0.0)
    returns_df.columns = syms
    print("Concated")
    print(returns_df.tail())

    returns_df = returns_df.groupby(returns_df.columns, axis=1).sum()
    print("Grouped")
    print(returns_df.tail())

    weights = portf_weights(data=returns_df.pct_change().dropna())
    print("weights")
    print(weights)

    #except Exception as e:
        #print(colored.red("At min_variance ".format(e)))
