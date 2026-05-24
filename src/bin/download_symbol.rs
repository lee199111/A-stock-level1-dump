use anyhow::{bail, Result};
use chrono::{Datelike, Local};
use futures::stream::{self, StreamExt};
use std::collections::HashMap;
use std::sync::Arc;
use tracing::{info, warn};

use stock_fetcher::{config, db, fetcher, parser, utils};
use stock_fetcher::models::MarketData;

const AUTO_CONCURRENT: usize = 8;
const AUTO_EMPTY_STOP_DAYS: usize = 30;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter("info")
        .init();

    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("用法: {} <股票/ETF代码> <YYYYMMDD> [--force]", args[0]);
        eprintln!("      {} <股票/ETF代码> <开始YYYYMMDD> <结束YYYYMMDD> [--force]", args[0]);
        eprintln!("      {} <股票/ETF代码> auto [--force]", args[0]);
        eprintln!("示例: {} 600519 20240101", args[0]);
        eprintln!("      {} 510500 20240102 20260225", args[0]);
        eprintln!("      {} 510500 auto", args[0]);
        eprintln!("      {} 510300 20240101 --force", args[0]);
        std::process::exit(1);
    }

    let code = args[1].trim();
    if code.len() != 6 || !code.chars().all(|c| c.is_ascii_digit()) {
        bail!("无效代码: {}", code);
    }

    let auto_mode = args[2] == "auto";
    let start_date: u32 = if auto_mode { 0 } else { args[2].parse()? };
    let has_end_date = !auto_mode && args.get(3).is_some_and(|s| s != "--force");
    let end_date: u32 = if auto_mode {
        0
    } else if has_end_date {
        args[3].parse()?
    } else {
        start_date
    };
    let force = args.iter().skip(3).any(|s| s == "--force");

    if !auto_mode && start_date > end_date {
        bail!("开始日期不能晚于结束日期: {} > {}", start_date, end_date);
    }

    info!(
        "单标的下载: {}, 日期: {}, 模式: {}",
        code,
        if auto_mode {
            "auto".to_string()
        } else {
            format!("{}-{}", start_date, end_date)
        },
        if force { "强制" } else { "增量" }
    );

    let config = config::Config::load("config.toml")?;
    db::init_database(&config).await?;

    let calendar = parser::calendar::TradingCalendar::load(&config.data.trading_calendar)?;
    let db_client = db::ClickHouseClient::new(&config);
    let pool_size = if auto_mode { AUTO_CONCURRENT * 2 } else { 1 };
    let client = fetcher::HighPerfTcpClient::new(
        config.server.host.clone(),
        config.server.port,
        config.server.timeout_secs,
        pool_size,
    )?;
    let validator = utils::DataValidator::new();

    if auto_mode {
        let today = today_u32();
        let latest_calendar_day = latest_trading_day_on_or_before(&calendar, today)?;
        download_auto_backward(
            code,
            latest_calendar_day,
            &calendar,
            &db_client,
            Arc::new(client),
            Arc::new(validator),
            force,
        )
        .await?;
        return Ok(());
    }

    let dates = if has_end_date {
        parser::calendar::TradingCalendar::validate_date(start_date)?;
        parser::calendar::TradingCalendar::validate_date(end_date)?;
        calendar.get_trading_days(start_date, end_date)
    } else {
        parser::calendar::TradingCalendar::validate_date(start_date)?;
        if !calendar.is_trading_day(start_date) {
            bail!("{} 不是交易日", start_date);
        }
        vec![start_date]
    };

    if dates.is_empty() {
        warn!("{}-{} 范围内没有交易日", start_date, end_date);
        return Ok(());
    }

    let total_days = dates.len();
    let mut downloaded_days = 0usize;
    let mut skipped_days = 0usize;
    let mut empty_days = 0usize;
    let mut total_rows = 0usize;

    for (idx, date) in dates.into_iter().enumerate() {
        if !force && db_client.data_exists(code, date).await? {
            skipped_days += 1;
            info!("[{}/{}] {} {} 已存在，跳过", idx + 1, total_days, code, date);
            continue;
        }

        let raw = client.fetch(code, date).await?;
        let raw_count = raw.len();
        let data = utils::filter_valid_data(raw, &validator);
        let valid_count = data.len();

        if data.is_empty() {
            empty_days += 1;
            warn!("[{}/{}] {} {} 无有效数据，原始记录 {} 条", idx + 1, total_days, code, date, raw_count);
            continue;
        }

        db_client.insert_market_data(&data).await?;
        downloaded_days += 1;
        total_rows += valid_count;
        info!(
            "[{}/{}] 完成: {} {} 写入 {} 条记录，过滤 {} 条",
            idx + 1,
            total_days,
            code,
            date,
            valid_count,
            raw_count - valid_count
        );
    }

    info!(
        "汇总: 交易日 {}, 下载 {}, 跳过 {}, 空数据 {}, 写入 {} 条",
        total_days, downloaded_days, skipped_days, empty_days, total_rows
    );

    Ok(())
}

fn today_u32() -> u32 {
    let today = Local::now().date_naive();
    (today.year() as u32) * 10000 + today.month() * 100 + today.day()
}

fn latest_trading_day_on_or_before(
    calendar: &parser::calendar::TradingCalendar,
    date: u32,
) -> Result<u32> {
    calendar
        .all_trading_days()
        .iter()
        .rev()
        .copied()
        .find(|&d| d <= date)
        .ok_or_else(|| anyhow::anyhow!("交易日历中没有不晚于 {} 的交易日", date))
}

async fn download_auto_backward(
    code: &str,
    latest_calendar_day: u32,
    calendar: &parser::calendar::TradingCalendar,
    db_client: &db::ClickHouseClient,
    client: Arc<fetcher::HighPerfTcpClient>,
    validator: Arc<utils::DataValidator>,
    force: bool,
) -> Result<u32> {
    let dates: Vec<u32> = calendar
        .all_trading_days()
        .iter()
        .rev()
        .copied()
        .filter(|&d| d <= latest_calendar_day)
        .collect();

    if dates.is_empty() {
        bail!("交易日历中没有不晚于 {} 的交易日", latest_calendar_day);
    }

    info!(
        "auto倒序扫描: {}, 从 {} 开始，并发 {}, 连续空数据 {} 个交易日后停止",
        code, latest_calendar_day, AUTO_CONCURRENT, AUTO_EMPTY_STOP_DAYS
    );

    let mut downloaded_days = 0usize;
    let mut skipped_days = 0usize;
    let mut empty_days = 0usize;
    let mut total_rows = 0usize;
    let mut empty_streak = 0usize;
    let mut oldest_hit: Option<u32> = None;
    let mut latest_hit: Option<u32> = None;

    for chunk in dates.chunks(AUTO_CONCURRENT) {
        let mut by_date: HashMap<u32, DayResult> = HashMap::new();
        let mut fetch_dates = Vec::new();

        for &date in chunk {
            if !force && db_client.data_exists(code, date).await? {
                by_date.insert(date, DayResult::skipped(date));
            } else {
                fetch_dates.push(date);
            }
        }

        let code_owned = code.to_string();
        let fetched = stream::iter(fetch_dates)
            .map(|date| {
                let client = client.clone();
                let validator = validator.clone();
                let code = code_owned.clone();

                async move {
                    let raw = client.fetch(&code, date).await?;
                    let raw_count = raw.len();
                    let data = utils::filter_valid_data(raw, &validator);
                    Ok::<DayResult, anyhow::Error>(if data.is_empty() {
                        DayResult::empty(date, raw_count)
                    } else {
                        DayResult::data(date, raw_count, data)
                    })
                }
            })
            .buffer_unordered(AUTO_CONCURRENT)
            .collect::<Vec<_>>()
            .await;

        for result in fetched {
            let result = result?;
            by_date.insert(result.date, result);
        }

        for &date in chunk {
            let Some(result) = by_date.remove(&date) else {
                continue;
            };

            match result.kind {
                DayResultKind::Skipped => {
                    skipped_days += 1;
                    empty_streak = 0;
                    latest_hit.get_or_insert(date);
                    oldest_hit = Some(date);
                    info!("{} {} 已存在，跳过", code, date);
                }
                DayResultKind::Empty { raw_count } => {
                    empty_days += 1;
                    empty_streak += 1;
                    warn!(
                        "{} {} 无有效数据，原始记录 {} 条，连续空数据 {}/{}",
                        code, date, raw_count, empty_streak, AUTO_EMPTY_STOP_DAYS
                    );
                }
                DayResultKind::Data { raw_count, data } => {
                    let valid_count = data.len();
                    db_client.insert_market_data(&data).await?;
                    downloaded_days += 1;
                    total_rows += valid_count;
                    empty_streak = 0;
                    latest_hit.get_or_insert(date);
                    oldest_hit = Some(date);
                    info!(
                        "完成: {} {} 写入 {} 条记录，过滤 {} 条",
                        code,
                        date,
                        valid_count,
                        raw_count - valid_count
                    );
                }
            }

            if latest_hit.is_some() && empty_streak >= AUTO_EMPTY_STOP_DAYS {
                info!(
                    "停止: 已连续 {} 个交易日无有效数据，推定已到上市前区间",
                    AUTO_EMPTY_STOP_DAYS
                );
                info!(
                    "汇总: 下载 {}, 跳过 {}, 空数据 {}, 写入 {} 条，可用区间 {:?}-{:?}",
                    downloaded_days, skipped_days, empty_days, total_rows, oldest_hit, latest_hit
                );
                return Ok(total_rows as u32);
            }
        }
    }

    info!(
        "汇总: 下载 {}, 跳过 {}, 空数据 {}, 写入 {} 条，可用区间 {:?}-{:?}",
        downloaded_days, skipped_days, empty_days, total_rows, oldest_hit, latest_hit
    );
    Ok(total_rows as u32)
}

struct DayResult {
    date: u32,
    kind: DayResultKind,
}

enum DayResultKind {
    Skipped,
    Empty { raw_count: usize },
    Data { raw_count: usize, data: Vec<MarketData> },
}

impl DayResult {
    fn skipped(date: u32) -> Self {
        Self {
            date,
            kind: DayResultKind::Skipped,
        }
    }

    fn empty(date: u32, raw_count: usize) -> Self {
        Self {
            date,
            kind: DayResultKind::Empty { raw_count },
        }
    }

    fn data(date: u32, raw_count: usize, data: Vec<MarketData>) -> Self {
        Self {
            date,
            kind: DayResultKind::Data { raw_count, data },
        }
    }
}
