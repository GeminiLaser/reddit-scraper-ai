import sqlite3
import praw
import pandas as pd
import streamlit as st
import google.generativeai as genai
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import time

# --- Gemini Setup ---
genai.configure(api_key="XYZ")
model = genai.GenerativeModel("models/gemini-2.0-flash")

def summarize_post_and_comments(post_text, comments_text):
    prompt = f"""
You are an assistant summarizing Reddit discussions.

Post content:
\"\"\"{post_text}\"\"\"

Top comments:
\"\"\"{comments_text}\"\"\"

Summarize the discussion in 3-5 concise sentences.
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error: {e}"

# ========== Database ==========

def create_db():
    conn = sqlite3.connect('reddit_scraper1.db')
    c = conn.cursor()

    # Drop old tables and recreate
    c.execute('''DROP TABLE IF EXISTS search_queries;''')
    c.execute('''DROP TABLE IF EXISTS scraped_data;''')

    # Create tables with the necessary columns
    c.execute('''CREATE TABLE IF NOT EXISTS search_queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT,
        upvotes INTEGER,
        date_range INTEGER,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        email TEXT,
        subscription_days INTEGER,
        expiration_date TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS scraped_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        author TEXT,
        upvotes INTEGER,
        url TEXT,
        subreddit TEXT,
        date TEXT,
        content TEXT,
        comments TEXT,
        summary TEXT,
        search_query_id INTEGER,
        FOREIGN KEY (search_query_id) REFERENCES search_queries(id)
    )''')
    
    conn.commit()
    conn.close()

create_db()

# ========== Reddit Setup ==========

reddit = praw.Reddit(
    client_id='XYZ',
    client_secret='XYZ',
    user_agent='XYZ'
)

# ========== Database Functions ==========

def store_search_query(keyword, upvotes, date_range, email, subscription_days):
    conn = sqlite3.connect('reddit_scraper1.db')
    c = conn.cursor()

    expiration_date = datetime.utcnow() + timedelta(days=subscription_days)

    c.execute('''INSERT INTO search_queries (keyword, upvotes, date_range, email, subscription_days, expiration_date) 
                 VALUES (?, ?, ?, ?, ?, ?)''', 
              (keyword, upvotes, date_range, email, subscription_days, expiration_date))
    conn.commit()
    query_id = c.lastrowid
    conn.close()
    return query_id

def store_data_in_db(posts_data, search_query_id):
    conn = sqlite3.connect('reddit_scraper1.db')
    c = conn.cursor()
    for post in posts_data:
        try:
            c.execute('''INSERT INTO scraped_data (
                title, author, upvotes, url, subreddit, date, content, comments, summary, search_query_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                post['Title'], post['Author'], post['Upvotes'], post['URL'], post['Subreddit'],
                post['Date'].strftime('%Y-%m-%d %H:%M:%S'), post['Content'], post['Comments'],
                post['Summary'], search_query_id
            ))
        except Exception as e:
            print(f"DB Error: {e}")
    conn.commit()
    conn.close()

# ========== Scraper ==========

def scrape_data(keyword, upvotes, date_range):
    now = datetime.utcnow()
    since = now - timedelta(days=date_range)
    since_timestamp = int(since.timestamp())
    posts = reddit.subreddit('all').search(f'"{keyword}"', time_filter='all', limit=100, sort='new')

    posts_data = []
    for post in posts:
        if post.score >= upvotes and post.created_utc >= since_timestamp and post.is_self:
            post.comments.replace_more(limit=0)
            comments = [f"Author: {c.author.name if c.author else 'N/A'}\n{c.body}" for c in post.comments.list()]
            comments_text = "\n###COMMENT_SEPARATOR###\n".join(comments)
            posts_data.append({
                'Title': post.title,
                'Author': post.author.name if post.author else 'N/A',
                'Upvotes': post.score,
                'URL': post.url,
                'Subreddit': post.subreddit.display_name,
                'Date': pd.to_datetime(post.created_utc, unit='s'),
                'Content': post.selftext,
                'Comments': comments_text
            })
    return posts_data

def scrape_and_store(keyword, upvotes, date_range, email, subscription_days):
    posts_data = scrape_data(keyword, upvotes, date_range)
    search_query_id = store_search_query(keyword, upvotes, date_range, email, subscription_days)

    # Sort posts by upvotes to find top 3
    top_3_posts = sorted(posts_data, key=lambda x: x['Upvotes'], reverse=True)[:3]

    # Set summaries for top 3, empty for others
    for post in posts_data:
        if post in top_3_posts:
            trimmed_content = post['Content'][:1000]
            trimmed_comments = "\n\n".join(post['Comments'].split("###COMMENT_SEPARATOR###")[:3])
            post["Summary"] = summarize_post_and_comments(trimmed_content, trimmed_comments)
        else:
            post["Summary"] = None

    store_data_in_db(posts_data, search_query_id)

    # Send email immediately if email is provided
    if email:
        email_content = "Here are the top 3 summaries for your search:\n\n"
        for i, post in enumerate(top_3_posts):
            email_content += f"### {i+1}. {post['Title']}\n{post['Summary']}\n\n"

        send_email("Your First Reddit Summary", email_content, email)

    return top_3_posts, pd.DataFrame(posts_data)

# ========== Send Email Function ==========

def send_email(subject, body, to_email):
    from_email = "XYZ"
    from_password = "XYZ"

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject

    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(from_email, from_password)
        text = msg.as_string()
        server.sendmail(from_email, to_email, text)
        server.quit()
        print("Email sent successfully!")
    except Exception as e:
        print(f"Error: {e}")

# ========== Scheduled Task ==========

def send_periodic_email_updates():
    conn = sqlite3.connect('reddit_scraper1.db')
    c = conn.cursor()
    
    # Check for active subscriptions
    current_time = datetime.utcnow()
    c.execute('''SELECT id, email, expiration_date FROM search_queries WHERE expiration_date > ?''', (current_time,))
    
    active_subscriptions = c.fetchall()
    for subscription in active_subscriptions:
        query_id, email, expiration_date = subscription
        posts_data = get_top_posts_for_email(query_id)  # This should get top posts
        email_content = "Here are the top 3 summaries for your subscription:\n\n"
        for i, post in enumerate(posts_data):
            email_content += f"### {i+1}. {post['Title']}\n{post['Summary']}\n\n"
        
        # Send email to user
        send_email("Your Daily Reddit Summary", email_content, email)
    
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(send_periodic_email_updates, 'interval', days=1, start_date='2025-04-13 12:00:00')  # Send daily
scheduler.start()

# ========== Streamlit UI ==========

st.title("🔍 Reddit Keyword Scraper with Smart Summarization")

st.markdown("Search Reddit for any keyword. You'll get top 3 posts (with summaries).")

keyword = st.text_input("Enter keyword:", "AI News")
upvotes = st.number_input("Minimum Upvotes", 1, 10000, 3)
date_range = st.number_input("Days to Look Back", 1, 30, 1)
email = st.text_input("Enter your email (Optional):", "")
subscription_days = st.number_input("Subscription Duration (days, Optional):", 1, 30, 7)

if st.button("Scrape and Summarize"):
    top_3_posts, df = scrape_and_store(keyword, upvotes, date_range, email, subscription_days)

    if not df.empty:
        st.subheader("📊 Scraped Posts Table")
        st.write(df[['Title', 'Author', 'Upvotes', 'URL', 'Date']])

        st.subheader("📝 Summaries (Top 3 Only)")
        for i, post in enumerate(top_3_posts):
            st.markdown(f"### {i+1}. {post['Title']}")
            st.markdown(post['Summary'])
            st.markdown("---")
    else:
        st.warning("No posts found.")
