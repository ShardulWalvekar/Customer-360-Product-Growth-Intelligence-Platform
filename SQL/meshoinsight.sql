CREATE DATABASE meesho_growth_db;
USE meesho_growth_db;
-- 1. Customers Table
CREATE TABLE customers (
    customer_id VARCHAR(20) PRIMARY KEY,
    customer_name VARCHAR(100),
    customer_state VARCHAR(50),
    city VARCHAR(50),
    pincode VARCHAR(10),
    acquisition_channel VARCHAR(50),
    signup_date DATE
);

-- 2. Products Table
CREATE TABLE products (
    sku VARCHAR(20) PRIMARY KEY,
    product_name VARCHAR(150),
    product_category VARCHAR(50),
    subcategory VARCHAR(50),
    seller_id VARCHAR(20),
    supplier_price NUMERIC(10,2),
    base_price NUMERIC(10,2),
    discount_pct NUMERIC(5,2),
    product_rating NUMERIC(3,1),
    stock_qty INT
);

-- 3. Orders Header Table
CREATE TABLE orders (
    order_id VARCHAR(20) PRIMARY KEY,
    customer_id VARCHAR(20) REFERENCES customers(customer_id),
    order_date TIMESTAMP,
    customer_state VARCHAR(50),
    city VARCHAR(50),
    pincode VARCHAR(10),
    total_amount NUMERIC(10,2),
    payment_method VARCHAR(30),
    status VARCHAR(20)
);

-- 4. Sales Flat Analytics Line Items Table
CREATE TABLE sales_flat_analytics (
    order_id VARCHAR(20),
    customer_id VARCHAR(20),
    order_date TIMESTAMP,
    customer_state VARCHAR(50),
    city VARCHAR(50),
    pincode VARCHAR(10),
    sku VARCHAR(20) REFERENCES products(sku),
    product_category VARCHAR(50),
    seller_id VARCHAR(20),
    supplier_price NUMERIC(10,2),
    base_price NUMERIC(10,2),
    discount_pct NUMERIC(5,2),
    product_rating NUMERIC(3,1),
    units_sold INT,
    revenue NUMERIC(10,2),
    payment_method VARCHAR(30),
    status VARCHAR(20)
);

-- 5. Feature Interaction Events Table
CREATE TABLE feature_events (
    session_id VARCHAR(30),
    customer_id VARCHAR(20),
    feature_name VARCHAR(50),
    device_type VARCHAR(30),
    converted_flag INT,
    event_timestamp TIMESTAMP
);

-- Create Performance Indexes
CREATE INDEX idx_orders_cust ON orders(customer_id);
CREATE INDEX idx_sales_sku ON sales_flat_analytics(sku);
CREATE INDEX idx_events_cust ON feature_events(customer_id);

USE meesho_growth_db;

-- 1. Populate customers
INSERT INTO customers (customer_id, customer_name, customer_state, city, pincode, acquisition_channel, signup_date)
SELECT Customer_ID, Customer_Name, Customer_State, City, CAST(Pincode AS CHAR), Acquisition_Channel, STR_TO_DATE(Signup_Date, '%Y-%m-%d')
FROM meesho_customers;

-- 2. Populate products
INSERT INTO products (sku, product_name, product_category, subcategory, seller_id, supplier_price, base_price, discount_pct, product_rating, stock_qty)
SELECT SKU, Product_Name, Product_Category, Subcategory, Seller_ID, Supplier_Price, Base_Price, `Discount_%`, Product_Rating, Stock_Qty
FROM meesho_products;

-- 3. Populate orders
INSERT INTO orders (order_id, customer_id, order_date, customer_state, city, pincode, total_amount, payment_method, status)
SELECT Order_ID, Customer_ID, STR_TO_DATE(Order_Date, '%Y-%m-%d %H:%i:%s'), Customer_State, City, CAST(Pincode AS CHAR), Total_Amount, Payment_Method, Status
FROM meesho_orders;

-- 4. Populate sales_flat_analytics
INSERT INTO sales_flat_analytics (order_id, customer_id, order_date, customer_state, city, pincode, sku, product_category, seller_id, supplier_price, base_price, discount_pct, product_rating, units_sold, revenue, payment_method, status)
SELECT Order_ID, Customer_ID, STR_TO_DATE(Order_Date, '%Y-%m-%d %H:%i:%s'), Customer_State, City, CAST(Pincode AS CHAR), SKU, Product_Category, Seller_ID, Supplier_Price, Base_Price, `Discount_%`, Product_Rating, Units_Sold, Revenue, Payment_Method, Status
FROM meesho_sales_flat_analytics;

-- 5. Populate feature_events
USE meesho_growth_db;

INSERT INTO feature_events (
    session_id, 
    customer_id, 
    feature_name, 
    device_type, 
    converted_flag, 
    event_timestamp
)
SELECT 
    Session_ID, 
    Customer_ID, 
    Feature_Name, 
    Device_Type, 
    Converted_Flag, 
    CASE 
        -- Standard ISO format: 2025-12-06 06:01:00
        WHEN Timestamp LIKE '%-%' THEN STR_TO_DATE(Timestamp, '%Y-%m-%d %H:%i:%s')
        
        -- Slash format with seconds: 2/19/2025 8:30:00
        WHEN Timestamp LIKE '%/%' AND Timestamp LIKE '%:%:%' THEN STR_TO_DATE(Timestamp, '%c/%e/%Y %k:%i:%s')
        
        -- Slash format without seconds: 2/19/2025 8:30
        WHEN Timestamp LIKE '%/%' THEN STR_TO_DATE(Timestamp, '%c/%e/%Y %k:%i')
        
        ELSE NULL
    END AS event_timestamp
FROM meesho_feature_events;
SELECT COUNT(*) AS total_events FROM feature_events;

SELECT session_id, customer_id, feature_name, event_timestamp 
FROM feature_events 
LIMIT 5;
USE meesho_growth_db;

-- Customer 360 View (v_customer_360)

CREATE OR REPLACE VIEW v_customer_360 AS
WITH order_summary AS (
    SELECT 
        customer_id,
        COUNT(DISTINCT order_id) AS total_orders,
        SUM(CASE WHEN status = 'DELIVERED' THEN total_amount ELSE 0 END) AS net_monetary,
        MAX(order_date) AS last_order_date,
        MIN(order_date) AS first_order_date
    FROM orders
    GROUP BY customer_id
)
SELECT 
    c.customer_id,
    c.customer_name,
    c.customer_state,
    c.city,
    c.acquisition_channel,
    c.signup_date,
    COALESCE(os.total_orders, 0) AS frequency,
    COALESCE(os.net_monetary, 0) AS monetary_value,
    os.last_order_date,
    DATEDIFF('2026-01-01', os.last_order_date) AS recency_days
FROM customers c
LEFT JOIN order_summary os ON c.customer_id = os.customer_id;

-- Product Performance View (v_product_performance)

CREATE OR REPLACE VIEW v_product_performance AS
SELECT 
    p.sku,
    p.product_name,
    p.product_category,
    p.seller_id,
    p.supplier_price,
    p.base_price,
    p.product_rating,
    COALESCE(SUM(s.units_sold), 0) AS total_units_sold,
    COALESCE(SUM(s.revenue), 0) AS gross_revenue,
    COALESCE(SUM(CASE WHEN s.status = 'DELIVERED' THEN s.revenue ELSE 0 END), 0) AS delivered_revenue,
    COALESCE(SUM(CASE WHEN s.status = 'RTO_COMPLETE' THEN s.revenue ELSE 0 END), 0) AS rto_revenue,
    ROUND(
        COUNT(CASE WHEN s.status = 'RTO_COMPLETE' THEN 1 END) * 100.0 / NULLIF(COUNT(s.order_id), 0), 2
    ) AS rto_rate_pct,
    COALESCE(SUM((s.base_price * (1 - s.discount_pct/100.0) - s.supplier_price) * s.units_sold), 0) AS gross_profit
FROM products p
LEFT JOIN sales_flat_analytics s ON p.sku = s.sku
GROUP BY p.sku, p.product_name, p.product_category, p.seller_id, p.supplier_price, p.base_price, p.product_rating;


SELECT * FROM v_customer_360 ORDER BY monetary_value DESC LIMIT 5;

SELECT * FROM v_product_performance ORDER BY gross_revenue DESC LIMIT 5;

