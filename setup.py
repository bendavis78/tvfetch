from distutils.core import setup

setup(
    name='tvfetch',
    version='0.1-dev',
    py_modules=['tvfetch'],
    install_requires=[
        'transmissionrpc',
        'feedparser',
        'bencodepy',
        'pytvdbapi'
    ],
    entry_points={
        'console_scripts': [
            'tvfetch = tvfetch:main'
        ]
    },
    data_files=[('share/tvfetch', ['tvfetch.conf.example'])],
    license='Creative Commons Attribution-Noncommercial-Share Alike license',
)
